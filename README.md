# SaveLivo - API Test 0.1

This is an MVP, not a finished multi-platform downloader.

## What works
- GET `/api/health` returns status.
- GET `/ornek.mp4?indir=1` serves an original demo MP4 file.
- GET `/api/ornek` returns available demo media information.
- POST `/api/incele` recognizes major social platforms, but **does not download their videos**.
- For direct HTTPS MP4, WebM, MP3, M4A, MOV and OGG links on **exactly allowlisted source hosts**, the API can issue a short-lived signed download link, after rights confirmation and a media HEAD request.
- No account, login credentials, DRM bypass or private content extraction.

## Deploy to Render

1. Create a GitHub repository such as `savelivo-api`.
2. Upload `main.py`, `ornek.mp4`, `requirements.txt`, `render.yaml`, and this README to the root of that repository (do not upload the ZIP itself).
3. Go to https://dashboard.render.com and create a **Web Service** connected to this GitHub repository.
4. Choose the **Free** instance for testing, Python runtime.
5. Build command: `pip install -r requirements.txt`
6. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
7. Configure environment variables `CORS_ORIGINS`, `ALLOWED_MEDIA_HOSTS`, and `DOWNLOAD_TOKEN_SECRET` as shown in render.yaml. Set the secret to a securely generated value if not using a blueprint.
8. After deployment, open `https://YOUR-RENDER-URL.onrender.com/api/health`, then `https://YOUR-RENDER-URL.onrender.com/ornek.mp4?indir=1`.

## Security and limitations
- This is not ready for public high-traffic use. Add quotas/rate limits, per-user rate limiting at an edge, monitoring, egress cost limits, observability, abuse reporting, and integration tests before public promotion.
- HTTP redirect responses are rejected. The destination host must be in `ALLOWED_MEDIA_HOSTS` and must resolve only to globally routable addresses.
- A maximum of 40 MB is transferred through this demo proxy. Free hosting can spin down or throttle.
- Always confirm legal permission and the platform's terms before adding extractors. Do not use account credentials or bypass paywalls, private settings, or DRM.
- Netlify website has not been changed yet. Once Render API is online, connect the existing static site to this API and redeploy to the same Netlify project.
