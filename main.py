"""SaveLivo 0.3: video + photo + audio, with bounded public-media adapters.

Public extractors are experimental and disabled unless explicitly enabled.
No login, cookies, DRM bypass, or private content extraction.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

BASE = Path(__file__).resolve().parent
VIDEO = BASE / "ornek.mp4"
PHOTO = BASE / "ornek-fotograf.png"
MAX_SIZE = 40 * 1024 * 1024
MEDIA_EXT = {"mp4", "webm", "mp3", "m4a", "mov", "m4v", "ogg", "flac", "wav", "jpg", "jpeg", "png", "webp", "gif", "avif", "bmp"}
MIME = {"mp4": "video/mp4", "webm": "video/webm", "mp3": "audio/mpeg", "m4a": "audio/mp4", "mov": "video/quicktime", "m4v": "video/x-m4v", "ogg": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif", "avif": "image/avif", "bmp": "image/bmp"}
ALLOWED_HOSTS = {x.strip().lower() for x in os.getenv("ALLOWED_MEDIA_HOSTS", "super-selkie-f65d2d.netlify.app").split(",") if x.strip()}
# Bu kaynak sadece halka acik, CC0 lisansli dogrudan MP4 testi icin eklenmistir.
# Rastgele alan adlarini acmayin: indirme endpointi bir vekil sunucudur.
ALLOWED_HOSTS.add("interactive-examples.mdn.mozilla.net")
# Direct public media can be added by the service administrator only.
# Platform pages use the separate, strictly bounded experimental adapters.
ALLOWED_HOSTS.add("upload.wikimedia.org")
# Explicit public image archives; no social-media hosts or arbitrary proxies.
PUBLIC_IMAGE_PATHS = {
    "science.nasa.gov": "/wp-content/uploads/",
    "images-assets.nasa.gov": "/image/",
    "esawebb.org": "/media/archives/images/",
}
CHECKED_IMAGE_EXT = {"jpg", "jpeg", "png"}
DIRECT_IMAGE_SLOTS = asyncio.Semaphore(3)
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "https://super-selkie-f65d2d.netlify.app").split(",") if x.strip()]
SECRET = os.getenv("DOWNLOAD_TOKEN_SECRET", "").encode("utf-8") or secrets.token_bytes(32)

app = FastAPI(title="SaveLivo API", version="0.3.1", docs_url="/api/docs")
from media_jobs import EXPERIMENTAL, detect_provider, owner_authenticated, router as social_router
app.include_router(social_router)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-SaveLivo-Test-Key"], allow_credentials=False)

PLATFORMS = {
    "Instagram": ("instagram.com",),
    "Facebook": ("facebook.com", "fb.watch"),
    "YouTube": ("youtube.com", "youtu.be"),
    "TikTok": ("tiktok.com",),
    "X (Twitter)": ("x.com", "twitter.com"),
    "Pinterest": ("pinterest.com", "pin.it"),
    "Reddit": ("reddit.com", "redd.it"),
    "Vimeo": ("vimeo.com",),
    "Dailymotion": ("dailymotion.com", "dai.ly"),
    "Twitch": ("twitch.tv", "clips.twitch.tv"),
    "Threads": ("threads.net", "threads.com"),
    "Snapchat": ("snapchat.com",),
    "SoundCloud": ("soundcloud.com",),
    "Tumblr": ("tumblr.com",),
}


def host_matches(host: str, root: str) -> bool:
    return host == root or host.endswith("." + root)


def platform_for(host: str) -> str | None:
    for name, domains in PLATFORMS.items():
        if any(host_matches(host, domain) for domain in domains):
            return name
    return None


def parse_media_url(value: str):
    try:
        p = urlsplit(value.strip())
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port
    except ValueError as ex:
        raise HTTPException(422, "Ge\u00e7ersiz video ba\u011flant\u0131s\u0131.") from ex
    if p.scheme != "https" or not host or p.username or p.password or port not in (None, 443):
        raise HTTPException(422, "Yaln\u0131zca g\u00fcvenli HTTPS ba\u011flant\u0131lar\u0131 desteklenir.")
    extension = p.path.rsplit(".", 1)[-1].lower() if "." in p.path else ""
    return p, host, extension


def verify_public_hostname(host: str, *, image_archive: bool = False) -> None:
    # The approved domain list is intentionally exact, not arbitrary hostnames.
    if host not in ALLOWED_HOSTS and not (image_archive and host in PUBLIC_IMAGE_PATHS):
        raise HTTPException(403, "Bu test sunucusunda yaln\u0131zca izin verilen dosya sunucular\u0131 a\u00e7\u0131kt\u0131r.")
    try:
        addr = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as ex:
        raise HTTPException(422, "Alan ad\u0131na ula\u015f\u0131lamad\u0131.") from ex
    if not addr or any(not ipaddress.ip_address(a[4][0]).is_global for a in addr):
        raise HTTPException(403, "Bu sunucu adresi g\u00fcvenlik nedeniyle engellendi.")


def verify_direct_source(p, host: str, extension: str) -> None:
    prefix = PUBLIC_IMAGE_PATHS.get(host)
    if prefix:
        path = unquote(p.path)
        if (extension not in CHECKED_IMAGE_EXT or not path.startswith(prefix)
                or p.query or p.fragment or "\\" in path
                or any(part in (".", "..") for part in path.split("/"))
                or any(ord(ch) < 32 for ch in path)):
            raise HTTPException(403, "Bu arsivde yalnizca dogrudan JPG/PNG dosyalari desteklenir.")
        verify_public_hostname(host, image_archive=True)
    else:
        verify_public_hostname(host)


def _image_type_ok(ctype: str, extension: str) -> bool:
    return ctype in (MIME[extension], "application/octet-stream")


def _check_image_markers(file, extension: str, total: int) -> None:
    # Validate basic JPEG/PNG file markers, not a full image decode.
    file.seek(0)
    header = file.read(8)
    if extension in ("jpg", "jpeg"):
        valid = total >= 4 and header.startswith(b"\xff\xd8\xff")
        if valid:
            file.seek(-2, os.SEEK_END)
            valid = file.read(2) == b"\xff\xd9"
    else:
        valid = total >= 20 and header == b"\x89PNG\r\n\x1a\n"
        if valid:
            file.seek(-12, os.SEEK_END)
            valid = file.read(12) == b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
    if not valid:
        raise HTTPException(422, "Kaynak gecerli veya eksiksiz bir JPG/PNG dosyasi donmedi.")
    file.seek(0)


async def _download_checked_image(url: str, extension: str, name: str):
    if DIRECT_IMAGE_SLOTS.locked():
        raise HTTPException(503, "Gorsel indirme kapasitesi dolu. Daha sonra tekrar dene.")
    file = tempfile.TemporaryFile(mode="w+b")
    try:
        async with DIRECT_IMAGE_SLOTS:
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
                    async with client.stream("GET", url, headers={"User-Agent": "SaveLivo/0.3", "Accept-Encoding": "identity"}) as resp:
                        if resp.status_code == 429:
                            raise HTTPException(429, "Kaynak gorsel isteklerini sinirladi. Daha sonra tekrar dene.")
                        if resp.status_code != 200:
                            raise HTTPException(422, "Kaynak gorseli indirmeye izin vermedi veya dosya bulunamadi.")
                        ctype = resp.headers.get("content-type", "").lower().split(";", 1)[0].strip()
                        if not _image_type_ok(ctype, extension):
                            raise HTTPException(422, "Kaynak beklenen gorsel turunu donmedi.")
                        if resp.headers.get("content-encoding", "identity").lower() != "identity":
                            raise HTTPException(422, "Beklenmeyen dosya kodlamasi.")
                        try:
                            expected = int(resp.headers.get("content-length", "0"))
                        except ValueError:
                            expected = 0
                        if expected > MAX_SIZE:
                            raise HTTPException(413, "Tek dosya siniri 40 MB.")
                        total = 0
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            total += len(chunk)
                            if total > MAX_SIZE:
                                raise HTTPException(413, "Tek dosya siniri 40 MB.")
                            file.write(chunk)
                        if expected > 0 and total != expected:
                            raise HTTPException(502, "Gorsel aktarimi tamamlanamadi.")
                _check_image_markers(file, extension, total)
    except (httpx.HTTPError, TimeoutError) as exc:
        file.close()
        raise HTTPException(502, "Kaynak gorsel aktarimi tamamlanamadi.") from exc
    except BaseException:
        file.close()
        raise

    def chunks():
        try:
            while chunk := file.read(65536):
                yield chunk
        finally:
            file.close()
    return StreamingResponse(chunks(), media_type=MIME[extension],
        headers={"Content-Length": str(total),
                 "Content-Disposition": f'attachment; filename="{name}"',
                 "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        background=BackgroundTask(file.close))


def issue_ticket(url: str) -> str:
    body = json.dumps({"u": url, "e": int(time.time()) + 300}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    signature = hmac.new(SECRET, encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def read_ticket(ticket: str) -> str:
    if len(ticket) > 3000 or "." not in ticket:
        raise HTTPException(403, "\u0130ndirme ba\u011flant\u0131s\u0131 ge\u00e7ersiz.")
    encoded, signature = ticket.rsplit(".", 1)
    valid = hmac.new(SECRET, encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, valid):
        raise HTTPException(403, "\u0130ndirme ba\u011flant\u0131s\u0131 ge\u00e7ersiz.")
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if int(payload["e"]) < time.time():
            raise HTTPException(403, "Ba\u011flant\u0131n\u0131n s\u00fcresi doldu; yeniden analiz et.")
        return str(payload["u"])
    except (ValueError, KeyError, TypeError) as ex:
        raise HTTPException(403, "\u0130ndirme ba\u011flant\u0131s\u0131 ge\u00e7ersiz.") from ex


class AnalyzeBody(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    izin_var: bool = False


@app.get("/api/health")
async def health():
    return {"durum": "calisiyor", "surum": "0.3.1", "ornek_video": True, "ornek_fotograf": True, "deneysel_platformlar_aktif": EXPERIMENTAL}


@app.get("/api/ornek")
async def sample_info(request: Request):
    base = str(request.base_url).rstrip("/")
    return {
        "durum": "hazir",
        "platform": "SaveLivo test videosu",
        "aciklama": "Bu video SaveLivo i\u00e7in olu\u015fturulmu\u015f \u00f6zg\u00fcn bir test dosyas\u0131d\u0131r.",
        "onizleme_url": base + "/ornek.mp4",
        "secenekler": [{"format": "MP4", "boyut_bayt": VIDEO.stat().st_size, "indir_url": base + "/ornek.mp4?indir=1"}],
    }


@app.get("/api/ornek-fotograf")
async def photo_info(request: Request):
    base = str(request.base_url).rstrip("/")
    return {"durum": "hazir", "platform": "SaveLivo fotograf testi",
            "aciklama": "Bu gorsel SaveLivo tarafindan olusturulan test dosyasidir.",
            "onizleme_url": base + "/ornek-fotograf.png",
            "secenekler": [{"format": "PNG", "boyut_bayt": PHOTO.stat().st_size,
                             "indir_url": base + "/ornek-fotograf.png?indir=1"}]}


@app.get("/ornek-fotograf.png")
async def sample_photo(indir: int = 0):
    return FileResponse(PHOTO, media_type="image/png", filename="savelivo-ornek-fotograf.png" if indir else None)


@app.get("/ornek.mp4")
async def sample_video(indir: int = 0):
    return FileResponse(VIDEO, media_type="video/mp4", filename="savelivo-ornek.mp4" if indir else None)


@app.post("/api/incele")
async def analyze(body: AnalyzeBody, request: Request):
    p, host, extension = parse_media_url(body.url)
    platform = platform_for(host)
    if platform:
        public = detect_provider(host)
        if public and public[0] in ("Instagram", "TikTok") and body.izin_var and owner_authenticated(request):
            return {"durum": "hazirlama_gerekli", "platform": platform,
                    "aciklama": ("Bu baglanti icin deneysel kamuya acik medya motoru denenebilir. "
                                 "Calismasi garanti degildir; sadece erisilebilir ve izinli icerikler."),
                    "secenekler": []}
        return {
            "durum": "izin_gerekli" if public and public[0] in ("Instagram", "TikTok") and EXPERIMENTAL and owner_authenticated(request) and not body.izin_var else "platform_tanindi",
            "platform": platform,
            "aciklama": (platform + " baglantisi tanindi. " +
                          ("Yonetici test modunda indirme iznini onayla." if public and EXPERIMENTAL and owner_authenticated(request)
                           else "Bu kaynaktan indirme aktif degil veya kaynak izinleri gerektiriyor.")),
            "secenekler": [],
        }
    if extension not in MEDIA_EXT:
        return {"durum": "desteklenmiyor", "platform": "Di\u011fer", "aciklama": "Bu ba\u011flant\u0131 do\u011frudan desteklenen bir medya dosyas\u0131 de\u011fil.", "secenekler": []}
    if not body.izin_var:
        return {"durum": "izin_gerekli", "platform": "Do\u011frudan medya", "aciklama": "\u0130ndirmek i\u00e7in i\u00e7erik sahibi oldu\u011funu veya indirme iznin bulundu\u011funu onayla.", "secenekler": []}
    verify_direct_source(p, host, extension)
    url = p.geturl()
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
            resp = await client.head(url, headers={"User-Agent": "SaveLivo/0.3"})
    except httpx.HTTPError as ex:
        raise HTTPException(502, "Kaynak sunucuya ula\u015f\u0131lamad\u0131.") from ex
    if resp.status_code != 200:
        raise HTTPException(422, "Dosya sunucusu indirmeye izin vermiyor veya dosya bulunamad\u0131.")
    ctype = resp.headers.get("content-type", "").lower().split(";", 1)[0]
    if not (ctype.startswith("video/") or ctype.startswith("audio/") or ctype.startswith("image/") or ctype == "application/octet-stream"):
        raise HTTPException(422, "Bu medya turu indirilebilir degil.")
    if ctype == "image/svg+xml":
        raise HTTPException(422, "SVG bu surumde desteklenmiyor.")
    if extension in CHECKED_IMAGE_EXT and not _image_type_ok(ctype, extension):
        raise HTTPException(422, "Kaynak beklenen gorsel turunu donmedi.")
    try:
        size = int(resp.headers.get("content-length", "0"))
    except ValueError:
        size = 0
    if size > MAX_SIZE:
        raise HTTPException(413, "Tek dosya siniri 40 MB.")
    return {
        "durum": "hazir", "platform": "Do\u011frudan medya", "aciklama": "Do\u011frudan medya dosyas\u0131 bulundu.",
        "secenekler": [{"format": extension.upper(), "boyut_bayt": size or None, "indir_url": "/api/indir/" + issue_ticket(url)}],
    }


@app.get("/api/indir/{ticket}")
async def download(ticket: str):
    url = read_ticket(ticket)
    p, host, extension = parse_media_url(url)
    if extension not in MEDIA_EXT:
        raise HTTPException(422, "Desteklenmeyen dosya t\u00fcr\u00fc.")
    verify_direct_source(p, host, extension)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", p.path.rsplit("/", 1)[-1])[:100] or ("video." + extension)
    if extension in CHECKED_IMAGE_EXT:
        # Preserve the suffix even when the original archive filename is long.
        name = (re.sub(r"[^A-Za-z0-9_-]", "_", unquote(p.path.rsplit("/", 1)[-1]).rsplit(".", 1)[0])[:85] or "SaveLivo") + "." + extension
        return await _download_checked_image(url, extension, name)
    # We refuse redirects and private hosts; untrusted URL proxying is disabled.
    async def streamer():
        consumed = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=60), follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": "SaveLivo/0.3"}) as resp:
                if resp.status_code != 200:
                    return
                typ = resp.headers.get("content-type", "").lower().split(";", 1)[0]
                if not (typ.startswith("video/") or typ.startswith("audio/") or typ.startswith("image/") or typ == "application/octet-stream") or typ == "image/svg+xml":
                    return
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    consumed += len(chunk)
                    if consumed > MAX_SIZE:
                        break
                    yield chunk
    return StreamingResponse(streamer(), media_type=MIME[extension], headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/", response_class=HTMLResponse)
async def homepage():
    return """<!doctype html><html lang='tr'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>SaveLivo API Test</title><style>
body{font:16px system-ui,sans-serif;background:#f7f7ff;color:#171832;max-width:700px;margin:55px auto;padding:0 18px}
main{background:white;padding:32px;border-radius:22px;box-shadow:0 10px 40px #0001}h1{margin-top:0}b{color:#6d3df5}
input[type=url]{width:100%;padding:14px;box-sizing:border-box;border:1px solid #d4d4e7;border-radius:10px;margin:12px 0}
button,a.btn{background:#6d3df5;color:white;border:0;border-radius:10px;padding:12px 16px;cursor:pointer;text-decoration:none;display:inline-block}
.small{color:#657085;font-size:13px;line-height:1.6}#output{margin-top:18px;padding:15px;border-radius:10px;background:#f3f0ff;white-space:pre-wrap;overflow-wrap:anywhere}
</style><main><h1>Save<b>Livo</b> API</h1>
<p>SaveLivo 0.3.1 beta: video, fotograf ve ses dosyalarinin baglanti analizi. Ornek dosyalar test amaclidir.</p>
<p><a class='btn' href='/ornek.mp4?indir=1'>Ornek MP4 indir</a> &nbsp; <a class='btn' href='/ornek-fotograf.png?indir=1'>Ornek PNG indir</a> &nbsp; <a href='/api/health'>Sunucu durumu</a></p>
<hr><h3>Baglanti testi</h3><form id='f'><input type='url' id='url' placeholder='https://...' required>
<label><input type='checkbox' id='rights'> Bu icerigi indirme hakkim veya iznim var.</label><p><button>Baglantiyi analiz et</button></p></form><div id='output' hidden></div>
<p class='small'>Not: Dogrudan medya indirmeleri izin verilen alan adlariyla sinirlidir. Sosyal platform adapterleri deneysel olup varsayilan olarak kapali durumdadir. Her platformda calisma garantisi yoktur.</p>
<script>document.querySelector('#f').onsubmit=async e=>{e.preventDefault();let d=document.querySelector('#output');d.hidden=false;d.textContent='Inceleniyor...';try{let r=await fetch('/api/incele',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value,izin_var:document.querySelector('#rights').checked})});let j=await r.json();d.textContent=j.aciklama||JSON.stringify(j);if(j.secenekler?.length){let a=document.createElement('a');a.href=j.secenekler[0].indir_url;a.textContent='Dosyayi indir';a.className='btn';d.append(document.createElement('br'),a)}}catch(e){d.textContent='Sunucu hatasi: '+e.message}};</script></main></html>"""
