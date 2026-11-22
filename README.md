# SaveLivo API - 0.3.0 beta

## Verified features in code and local tests

- The existing MP4 demo and its endpoints remain available.
- A new original PNG demo is provided at `/api/ornek-fotograf`.
- Direct HTTPS image/video/audio URLs from an administrator-controlled host allowlist can be checked with `/api/incele`.
- Experimental per-post platform adapters exist at `/api/hazirla`, `/api/isler/{id}`, and `/api/sonuc-dosyasi/{id}`.
- Adapters use `gallery-dl` for some public photo posts and `yt-dlp` for some public video posts, with bounded jobs. Their success on any specific social platform has NOT been verified live.

## Not supported or not guaranteed

- Private stories, login-required posts, paid material, DRM-protected media, or account/profile scraping.
- YouTube automatic downloads are NOT enabled. Platform-specific policy compliance must be evaluated first.
- A platform logo or recognized link never implies that its content can be downloaded.
- Production-ready rate limiting, per-user authentication, durable queues, or persistent object storage.

## Deployment (GitHub -> Render)

Upload these files to the **root** of the existing `savelivo-api` GitHub repository: `main.py`, `media_jobs.py`, `requirements.txt`, `render.yaml`, `ornek-fotograf.png`. Keep `ornek.mp4` in the root. Commit the change. In Render, choose **Manual Deploy -> Deploy latest commit**.

If Render uses existing service settings rather than the blueprint, its start command must stay:

`uvicorn main:app --host 0.0.0.0 --port $PORT`

Test:

- `https://savelivo-api.onrender.com/api/health` -> version `0.3.0`
- `https://savelivo-api.onrender.com/api/ornek` -> existing MP4 demo
- `https://savelivo-api.onrender.com/api/ornek-fotograf` -> PNG demo
- `https://savelivo-api.onrender.com/api/medya-yetenekleri` -> feature disclosure

**Important: `ENABLE_PUBLIC_EXTRACTORS` defaults to `false`.** Do not turn it on on a public website without first reviewing server egress/SSRF isolation, platform rules, bandwidth and abuse protections. The experimental adapter code is provided for a controlled development environment, not as a claim of universal downloading.

## Frontend (Netlify)

Upload the separate `SaveLivo-v03-Netlify.zip` in the existing Netlify project's Deploys page. It adds the PNG test button and honors the adapter's explicit enabled/disabled status.

## Operational limits

- Direct downloads: up to 40 MiB per file; only HTTPS, no redirects, no private IPs; permitted hosts configured by administrator.
- Experimental platform jobs: one active task, up to 8 files, 30 MiB per item, 100 MiB per job, 75 seconds per job; jobs expire after 15 minutes.
- Temporary files are deleted opportunistically and may disappear after a Render Free restart. Free Render is suitable for tests, not large-scale production downloads.

## Privacy and rights

Do not enter user passwords, cookies, or private account tokens. Only download content you own, are permitted to access and download, and whose terms allow the operation. No DRM or access-control bypass is included.
