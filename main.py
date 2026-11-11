"""SaveLivo API MVP: demo + allowlisted direct media links only.

No login, DRM, private post extraction, or social platform scraping.
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
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
VIDEO = BASE / "ornek.mp4"
MAX_SIZE = 40 * 1024 * 1024
MEDIA_EXT = {"mp4", "webm", "mp3", "m4a", "mov", "ogg"}
MIME = {"mp4": "video/mp4", "webm": "video/webm", "mp3": "audio/mpeg", "m4a": "audio/mp4", "mov": "video/quicktime", "ogg": "audio/ogg"}
ALLOWED_HOSTS = {x.strip().lower() for x in os.getenv("ALLOWED_MEDIA_HOSTS", "super-selkie-f65d2d.netlify.app").split(",") if x.strip()}
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "https://super-selkie-f65d2d.netlify.app").split(",") if x.strip()]
SECRET = os.getenv("DOWNLOAD_TOKEN_SECRET", "").encode("utf-8") or secrets.token_bytes(32)

app = FastAPI(title="SaveLivo API", version="0.1.0", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["Content-Type"], allow_credentials=False)

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


def verify_public_hostname(host: str) -> None:
    # The approved domain list is intentionally exact, not arbitrary hostnames.
    if host not in ALLOWED_HOSTS:
        raise HTTPException(403, "Bu test sunucusunda yaln\u0131zca izin verilen dosya sunucular\u0131 a\u00e7\u0131kt\u0131r.")
    try:
        addr = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as ex:
        raise HTTPException(422, "Alan ad\u0131na ula\u015f\u0131lamad\u0131.") from ex
    if not addr or any(not ipaddress.ip_address(a[4][0]).is_global for a in addr):
        raise HTTPException(403, "Bu sunucu adresi g\u00fcvenlik nedeniyle engellendi.")


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
    return {"durum": "calisiyor", "surum": "0.1.0", "ornek_video": True}


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


@app.get("/ornek.mp4")
async def sample_video(indir: int = 0):
    return FileResponse(VIDEO, media_type="video/mp4", filename="savelivo-ornek.mp4" if indir else None)


@app.post("/api/incele")
async def analyze(body: AnalyzeBody):
    p, host, extension = parse_media_url(body.url)
    platform = platform_for(host)
    if platform:
        return {
            "durum": "platform_tanindi",
            "platform": platform,
            "aciklama": (platform + " ba\u011flant\u0131s\u0131 tan\u0131nd\u0131. Bu test s\u00fcr\u00fcm\u00fc sosyal medya videolar\u0131n\u0131 hen\u00fcz indirmiyor; her servis ayr\u0131ca, izinleri ve kullan\u0131m ko\u015fullar\u0131 kontrol edilerek eklenecek."),
            "secenekler": [],
        }
    if extension not in MEDIA_EXT:
        return {"durum": "desteklenmiyor", "platform": "Di\u011fer", "aciklama": "Bu ba\u011flant\u0131 do\u011frudan desteklenen bir medya dosyas\u0131 de\u011fil.", "secenekler": []}
    if not body.izin_var:
        return {"durum": "izin_gerekli", "platform": "Do\u011frudan medya", "aciklama": "\u0130ndirmek i\u00e7in i\u00e7erik sahibi oldu\u011funu veya indirme iznin bulundu\u011funu onayla.", "secenekler": []}
    verify_public_hostname(host)
    url = p.geturl()
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
            resp = await client.head(url, headers={"User-Agent": "SaveLivo-MVP/0.1"})
    except httpx.HTTPError as ex:
        raise HTTPException(502, "Kaynak sunucuya ula\u015f\u0131lamad\u0131.") from ex
    if resp.status_code != 200:
        raise HTTPException(422, "Dosya sunucusu indirmeye izin vermiyor veya dosya bulunamad\u0131.")
    ctype = resp.headers.get("content-type", "").lower().split(";", 1)[0]
    if not (ctype.startswith("video/") or ctype.startswith("audio/") or ctype == "application/octet-stream"):
        raise HTTPException(422, "Bu ba\u011flant\u0131da indirilir bir medya dosyas\u0131 bulunamad\u0131.")
    try:
        size = int(resp.headers.get("content-length", "0"))
    except ValueError:
        size = 0
    if size > MAX_SIZE:
        raise HTTPException(413, "Test s\u00fcr\u00fcm\u00fc en fazla 40 MB dosya indirir.")
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
    verify_public_hostname(host)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", p.path.rsplit("/", 1)[-1])[:100] or ("video." + extension)
    # We refuse redirects and private hosts; untrusted URL proxying is disabled.
    async def streamer():
        consumed = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=60), follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": "SaveLivo-MVP/0.1"}) as resp:
                if resp.status_code != 200:
                    return
                typ = resp.headers.get("content-type", "").lower().split(";", 1)[0]
                if not (typ.startswith("video/") or typ.startswith("audio/") or typ == "application/octet-stream"):
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
<p>Gercek indirme altyapisinin ilk test surumu calisiyor. Ornek video test icin uretilmistir.</p>
<p><a class='btn' href='/ornek.mp4?indir=1'>Ornek MP4 videosunu indir</a> &nbsp; <a href='/api/health'>Sunucu durumu</a></p>
<hr><h3>Baglanti testi</h3><form id='f'><input type='url' id='url' placeholder='https://...' required>
<label><input type='checkbox' id='rights'> Bu icerigi indirme hakkim veya iznim var.</label><p><button>Baglantiyi analiz et</button></p></form><div id='output' hidden></div>
<p class='small'>Not: Bu surum sosyal medya videolarini indirmez. Yalnizca izin verilen alan adlarindaki dogrudan medya dosyalarini test eder. Ilk test icin yukaridaki ornek video kullanilabilir.</p>
<script>document.querySelector('#f').onsubmit=async e=>{e.preventDefault();let d=document.querySelector('#output');d.hidden=false;d.textContent='Inceleniyor...';try{let r=await fetch('/api/incele',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value,izin_var:document.querySelector('#rights').checked})});let j=await r.json();d.textContent=j.aciklama||JSON.stringify(j);if(j.secenekler?.length){let a=document.createElement('a');a.href=j.secenekler[0].indir_url;a.textContent='Dosyayi indir';a.className='btn';d.append(document.createElement('br'),a)}}catch(e){d.textContent='Sunucu hatasi: '+e.message}};</script></main></html>"""
