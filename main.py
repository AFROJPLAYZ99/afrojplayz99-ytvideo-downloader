import os, uuid, time, shutil, threading, subprocess
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import yt_dlp

APP_DIR=Path(__file__).resolve().parent
WORK=Path(os.getenv("DOWNLOAD_DIR","/tmp/afroj-jobs"))
WORK.mkdir(parents=True,exist_ok=True)
MAX_JOBS=2
jobs={}
lock=threading.Lock()
app=FastAPI(title="AFROJ Video Downloader API",version="1.1.0")
origins=[x.strip() for x in os.getenv("ALLOWED_ORIGINS","*").split(",") if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=False,allow_methods=["GET","POST"],allow_headers=["*"])

class InspectRequest(BaseModel):
    url:str
    authorized:bool=False
class JobRequest(BaseModel):
    url:str
    authorized:bool=False
    mode:str="video"
    quality:str="best"
    start:int=Field(ge=0)
    end:int=Field(gt=0)

def validate(req):
    if not req.authorized:
        raise HTTPException(403,"Confirm that you own this video or have permission to download it.")
    try:
        u=urlparse(req.url.strip())
    except Exception:
        raise HTTPException(400,"Invalid URL.")
    host=(u.hostname or "").lower()
    if u.scheme not in ("http","https") or not (host=="youtu.be" or host=="youtube.com" or host.endswith(".youtube.com") or host=="youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")):
        raise HTTPException(400,"Only standard YouTube video URLs are accepted.")
    if not u.path or u.path=="/":
        raise HTTPException(400,"A video URL is required.")
    return req.url.strip()

def ydl_base():
    return {
        "quiet":True,
        "no_warnings":False,
        "noplaylist":True,
        "ignoreerrors":False,
        "socket_timeout":25,
        "retries":2,
        "fragment_retries":2,
        "extractor_retries":2,
        "max_filesize":1500*1024*1024,
        "restrictfilenames":True,
        # Prefer IPv4 on hosts where the IPv6 route is unreliable.
        "source_address":"0.0.0.0",
    }

@app.get("/health")
def health():
    deno=shutil.which("deno")
    return {
        "ok":True,
        "service":"afroj-video-downloader",
        "version":"1.1.0",
        "yt_dlp_version":getattr(yt_dlp,"version",{}).get("__version__","unknown"),
        "deno_available":bool(deno),
        "active_jobs":sum(1 for j in jobs.values() if j["status"] in ("queued","processing"))
    }

@app.post("/api/info")
def info(req:InspectRequest):
    url=validate(req)
    try:
        with yt_dlp.YoutubeDL(ydl_base()) as ydl:
            data=ydl.extract_info(url,download=False)
        if not data: raise ValueError("No video metadata returned.")
        if data.get("is_live"): raise HTTPException(400,"Live streams are not supported by this clip workflow.")
        duration=int(data.get("duration") or 0)
        if duration<=0: raise HTTPException(400,"This video has no fixed duration.")
        formats=data.get("formats") or []
        heights=sorted({int(f["height"]) for f in formats if f.get("height") and f.get("vcodec")!="none"},reverse=True)
        return {"title":data.get("title","YouTube video"),"duration":duration,"available_heights":heights}
    except HTTPException: raise
    except Exception as e:
        msg=str(e)
        if "Sign in to confirm" in msg or "not a bot" in msg.lower():
            raise HTTPException(422,"YouTube is refusing requests from this server (bot verification/IP restriction). Deno and yt-dlp EJS support are installed in this backend build, but YouTube may still block datacenter IPs. Try later or use a creator-authorized source file.")
        raise HTTPException(422,"Could not inspect this video. It may be unavailable, restricted, or unsupported. "+msg[:180])

def run_job(job_id,req,url):
    folder=WORK/job_id
    folder.mkdir(parents=True,exist_ok=True)
    def progress(d):
        with lock:
            if job_id not in jobs:return
            if d.get("status")=="downloading":
                total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done=d.get("downloaded_bytes") or 0
                jobs[job_id]["progress"]=min(92,int(done*92/total)) if total else min(85,jobs[job_id]["progress"]+1)
                jobs[job_id]["message"]="Downloading video…"
            elif d.get("status")=="finished":
                jobs[job_id]["progress"]=94;jobs[job_id]["message"]="Finalizing output…"
    try:
        with lock: jobs[job_id].update(status="processing",message="Starting download…")
        output=str(folder/"source.%(ext)s")
        opts=ydl_base()
        opts.update({"outtmpl":output,"progress_hooks":[progress],"continuedl":True})
        if req.mode=="audio":
            opts.update({"format":"bestaudio/best","postprocessors":[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]})
        else:
            height={"best":1080,"720":720,"480":480,"360":360}.get(req.quality,1080)
            opts["format"]=f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best[height<={height}]"
            opts["merge_output_format"]="mp4"
        with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([url])
        candidates=[p for p in folder.iterdir() if p.is_file() and p.name!="final.mp4"]
        if not candidates: raise RuntimeError("Downloader did not create an output file.")
        source=max(candidates,key=lambda p:p.stat().st_size)
        if req.mode=="audio":
            final=source
        elif req.start>0 or req.end:
            if req.end<=req.start: raise RuntimeError("Invalid clip range.")
            final=folder/"final.mp4"
            cmd=["ffmpeg","-y","-ss",str(req.start),"-i",str(source),"-t",str(req.end-req.start),"-c","copy","-movflags","+faststart",str(final)]
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=240)
            if result.returncode!=0:
                cmd=["ffmpeg","-y","-ss",str(req.start),"-i",str(source),"-t",str(req.end-req.start),"-c:v","libx264","-preset","veryfast","-crf","23","-c:a","aac","-movflags","+faststart",str(final)]
                result=subprocess.run(cmd,capture_output=True,text=True,timeout=480)
                if result.returncode!=0: raise RuntimeError("FFmpeg could not create the selected clip.")
            source.unlink(missing_ok=True)
        else:
            final=source
        with lock:
            jobs[job_id].update(status="complete",progress=100,message="Ready to download.",file=str(final),filename=final.name)
    except Exception as e:
        msg=str(e)
        if "Sign in to confirm" in msg or "not a bot" in msg.lower():
            msg="YouTube refused this server request (bot verification/IP restriction). This cannot be fixed reliably by retrying. Use a permitted creator download/source file or retry later."
        with lock: jobs[job_id].update(status="error",message=("Processing failed: "+msg)[:500],progress=0)
        shutil.rmtree(folder,ignore_errors=True)

@app.post("/api/jobs")
def create_job(req:JobRequest):
    url=validate(req)
    if req.mode not in ("video","audio"): raise HTTPException(400,"Unsupported output mode.")
    if req.end<=req.start: raise HTTPException(400,"End time must be after start time.")
    if req.end-req.start>4*60*60: raise HTTPException(400,"Maximum clip duration is 4 hours.")
    with lock:
        active=sum(1 for j in jobs.values() if j["status"] in ("queued","processing"))
        if active>=MAX_JOBS: raise HTTPException(429,"Server is busy. Please try again in a minute.")
        job_id=uuid.uuid4().hex
        jobs[job_id]={"status":"queued","progress":0,"message":"Waiting for worker…","created":time.time(),"file":None,"filename":None}
    threading.Thread(target=run_job,args=(job_id,req,url),daemon=True).start()
    return {"job_id":job_id}

@app.get("/api/jobs/{job_id}")
def job_status(job_id:str):
    with lock: j=jobs.get(job_id)
    if not j: raise HTTPException(404,"Job not found or expired.")
    return {k:j[k] for k in ("status","progress","message","filename")}

@app.get("/api/jobs/{job_id}/file")
def get_file(job_id:str):
    with lock: j=jobs.get(job_id)
    if not j or j["status"]!="complete" or not j.get("file"): raise HTTPException(404,"File is not ready.")
    p=Path(j["file"])
    if not p.exists(): raise HTTPException(410,"The temporary download has expired. Please process it again.")
    media="audio/mpeg" if p.suffix.lower()==".mp3" else "video/mp4"
    return FileResponse(p,media_type=media,filename=("AFROJ-"+p.name))

def cleanup_loop():
    while True:
        time.sleep(300)
        now=time.time()
        with lock:
            expired=[k for k,v in jobs.items() if now-v["created"]>3600 and v["status"] not in ("queued","processing")]
            for k in expired: jobs.pop(k,None)
        for k in expired: shutil.rmtree(WORK/k,ignore_errors=True)
threading.Thread(target=cleanup_loop,daemon=True).start()
