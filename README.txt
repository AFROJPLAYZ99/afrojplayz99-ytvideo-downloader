AFROJ backend refresh (v1.1.0)

Upload these four files to the ROOT of the existing GitHub backend repository:
- main.py
- requirements.txt
- Dockerfile
- render.yaml

Commit the changes. Render should detect the GitHub commit and redeploy the Docker service. If auto-deploy is off, press Manual Deploy -> Deploy latest commit.

After deployment, open:
https://YOUR-RENDER-SERVICE/health

Expected fields include:
{"ok":true,"service":"afroj-video-downloader","version":"1.1.0","yt_dlp_version":"...","deno_available":true,"active_jobs":0}

What changed:
- Installs Deno in the Docker image.
- Installs yt-dlp with its [default] extra, which includes yt-dlp-ejs.
- Uses IPv4 for the outbound yt-dlp connection.
- Adds backend diagnostics and a clearer bot-wall error.

Important: Deno/EJS supports yt-dlp's documented YouTube JavaScript challenge handling. It cannot guarantee access when YouTube blocks the Render/datacenter IP itself. Do not upload personal browser cookies or account credentials to a public server.
