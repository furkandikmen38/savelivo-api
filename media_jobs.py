"""Experimental, bounded public-media jobs for SaveLivo.

Disabled by default. This is not a login, DRM, or private-media downloader.
Designed for a small single-worker preview, not an unrestricted production service.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api")
LOG = logging.getLogger("uvicorn.error")
# Two independent switches: never open an unprotected downloader on the public API.
OWNER_KEY = os.environ.get("SAVELIVO_TEST_KEY", "").strip()
EXPERIMENTAL = (os.environ.get("ENABLE_PUBLIC_EXTRACTORS", "false").lower() == "true" and len(OWNER_KEY) >= 24)
TEST_PROVIDERS = {"Instagram", "TikTok"}


def owner_authenticated(request: Request) -> bool:
    candidate = request.headers.get("X-SaveLivo-Test-Key", "")
    return bool(EXPERIMENTAL and candidate and hmac.compare_digest(candidate, OWNER_KEY))


def require_owner(request: Request) -> None:
    if not EXPERIMENTAL:
        raise HTTPException(503, "Ozel platform testi kapali. Yonetici ayarlari gerekli.")
    if not owner_authenticated(request):
        raise HTTPException(403, "Yonetici test anahtari hatali veya eksik.")
MAX_TASKS = 3
MAX_ITEMS = 8
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_SINGLE_BYTES = 30 * 1024 * 1024
TASK_TTL = 15 * 60
TASK_TIMEOUT = 75
MEDIA_SUFFIXES = {".mp4", ".m4v", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".mp3", ".m4a", ".wav", ".ogg", ".flac"}

# A provider appearing here is eligible for an *attempt*, not guaranteed support.
# We intentionally exclude YouTube while its platform-specific permissions
# and compliant integration options are evaluated.
PROVIDERS = {
    "Instagram": (("instagram.com",), "gallery"),
    "TikTok": (("tiktok.com",), "video"),
    "Pinterest": (("pinterest.com", "pin.it"), "gallery"),
    "Reddit": (("reddit.com", "redd.it"), "gallery"),
    "Vimeo": (("vimeo.com",), "video"),
    "Dailymotion": (("dailymotion.com", "dai.ly"), "video"),
    "Twitch": (("twitch.tv", "clips.twitch.tv"), "video"),
    "X (Twitter)": (("x.com", "twitter.com"), "gallery"),
    "Threads": (("threads.net", "threads.com"), "gallery"),
    "Facebook": (("facebook.com", "fb.watch"), "video"),
    "Tumblr": (("tumblr.com",), "gallery"),
}

@dataclass
class Job:
    id: str
    url: str
    platform: str
    created: float = field(default_factory=time.time)
    status: str = "queued"
    description: str = ""
    directory: Path | None = None
    result: Path | None = None
    count: int = 0

JOBS: dict[str, Job] = {}
ACTIVE = 0
RECENT: dict[str, list[float]] = {}

class StartBody(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    izin_var: bool = False


def _match(host: str, root: str) -> bool:
    return host == root or host.endswith("." + root)


def detect_provider(host: str):
    for name, (hosts, engine) in PROVIDERS.items():
        if any(_match(host, x) for x in hosts):
            return name, engine
    return None


def _validate_url(raw: str):
    try:
        p = urlsplit(raw.strip())
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port
    except ValueError as exc:
        raise HTTPException(422, "Gecersiz baglanti.") from exc
    if p.scheme != "https" or not host or port not in (None, 443) or p.username or p.password or p.fragment:
        raise HTTPException(422, "Yalnizca gecerli HTTPS baglantilari kullanilabilir.")
    provider = detect_provider(host)
    if not provider or provider[0] not in TEST_PROVIDERS:
        raise HTTPException(422, "Ozel test su anda yalnizca Instagram ve TikTok gonderileri icin acik.")
    try:
        addresses = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise HTTPException(403, "Guvenli olmayan sunucu adresi reddedildi.")
    except OSError as exc:
        raise HTTPException(422, "Platform adresi dogrulanamadi.") from exc
    # Permit individual media posts only; never entire profiles, timelines, or search feeds.
    path = (p.path or "/").strip("/")
    parts = path.split("/") if path else []
    name = provider[0]
    if name == "Instagram":
        # Only individual posts/reels. Stories generally need authenticated access,
        # which this no-login beta intentionally does not provide.
        safe = (len(parts) == 2 and parts[0] in ("p", "reel", "reels", "tv") and len(parts[1]) >= 4)
    elif name == "TikTok":
        safe = (len(parts) == 3 and parts[0].startswith("@") and parts[1] == "video" and parts[2].isdigit()) or (len(parts) == 1 and host.startswith(("vm.", "vt.")))
    elif name == "Pinterest":
        safe = (len(parts) >= 2 and parts[0] == "pin") or (host.endswith("pin.it") and len(parts) == 1)
    elif name == "Reddit":
        safe = "comments" in parts or host == "redd.it"
    elif name == "Vimeo":
        safe = bool(parts and parts[-1].isdigit())
    elif name == "Dailymotion":
        safe = (len(parts) >= 2 and parts[0] == "video") or (host == "dai.ly" and len(parts) == 1)
    elif name == "Twitch":
        safe = (len(parts) >= 2 and parts[0] in ("videos", "clip")) or ("clip" in parts) or host == "clips.twitch.tv"
    elif name == "X (Twitter)":
        safe = len(parts) >= 3 and parts[1] == "status"
    elif name == "Threads":
        safe = len(parts) >= 3 and parts[1] == "post"
    elif name == "Facebook":
        safe = bool(parts and (parts[0] in ("reel", "watch", "videos", "share", "photo.php") or "videos" in parts))
    else:
        safe = len(parts) >= 2 and parts[-1].isdigit()
    if not safe:
        raise HTTPException(422, "Tek bir gonderi, video veya hikaye baglantisi gir. Profil ve toplu arama desteklenmiyor.")
    return p.geturl(), provider


def _cleanup():
    now = time.time()
    for key, job in list(JOBS.items()):
        if now - job.created >= TASK_TTL and job.status not in ("queued", "working"):
            JOBS.pop(key, None)
            if job.directory:
                shutil.rmtree(job.directory, ignore_errors=True)
    for key, values in list(RECENT.items()):
        RECENT[key] = [x for x in values if now - x <= 60]
        if not RECENT[key]:
            RECENT.pop(key, None)


def _safe_extractor_note(stderr_text: str) -> str:
    """Keep one redacted diagnostic line without URLs or credentials."""
    lines = [line.strip() for line in stderr_text.splitlines() if line.strip()]
    important = ("error", "failed", "forbidden", "429", "403", "login", "sign in",
                 "unavailable", "unsupported", "unable", "blocked", "warning")
    selected = [line for line in lines if any(word in line.lower() for word in important)]
    line = (selected[-1] if selected else (lines[-1] if lines else "no stderr output"))
    line = re.sub(r"https?://[^\s<>\"']+", "[url]", line, flags=re.IGNORECASE)
    line = re.sub(r"(?i)(authorization|cookie|token|secret|api[_-]?key|password)\s*[:=]\s*[^\s;,]+", r"\1=[redacted]", line)
    return line[:240]


def _run_command(job: Job, engine: str):
    base = Path(tempfile.mkdtemp(prefix="savelivo-"))
    job.directory = base
    target = base / "indirilenler"
    target.mkdir()
    if engine == "gallery":
        args = [
            sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-part", "--no-mtime",
            "--no-postprocessors", "--http-timeout", "12", "--retries", "1",
            "--range", "1-8", "--filesize-max", "30M", "--destination", str(target),
            job.url,
        ]
    else:
        args = [
            sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-progress", "--no-warnings",
            "--restrict-filenames", "--no-part", "--no-mtime", "--no-cache-dir", "--no-playlist",
            "--max-downloads", "1", "--max-filesize", "30M", "--socket-timeout", "12",
            "--retries", "1", "--fragment-retries", "1", "--format", "b[ext=mp4]/b",
            "--output", str(target / "%(title).60s-%(id)s.%(ext)s"), job.url,
        ]
    # Use a temporary file so verbose extractor output cannot grow in memory.
    exit_status = -1
    failure_note = "no stderr output"
    try:
        # Never forward user-supplied cookies, profiles or credentials to a provider.
        # Child processes must never inherit API credentials or Render secrets.
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
               "LANG": "C.UTF-8", "HOME": str(base),
               "XDG_CONFIG_HOME": str(base), "XDG_CACHE_HOME": str(base),
               "PYTHONIOENCODING": "utf-8"}
        for optional in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "SSL_CERT_DIR"):
            if os.environ.get(optional):
                env[optional] = os.environ[optional]
        with tempfile.TemporaryFile() as diagnostic_output:
            process = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=diagnostic_output,
                                     timeout=TASK_TIMEOUT, check=False, cwd=base, env=env)
            exit_status = process.returncode
            diagnostic_output.seek(0, os.SEEK_END)
            diagnostic_output.seek(max(0, diagnostic_output.tell() - 12000))
            failure_note = _safe_extractor_note(diagnostic_output.read().decode("utf-8", "replace"))
    except subprocess.TimeoutExpired as exc:
        LOG.error("SaveLivo extraction timeout platform=%s engine=%s", job.platform, engine)
        raise ValueError("Islem suresi doldu. Daha kisa veya kucuk bir gonderi dene.") from exc
    root = target.resolve()
    files = []
    total = 0
    for p in target.rglob("*"):
        if not p.is_file() or p.is_symlink() or p.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if not p.resolve().is_relative_to(root):
            continue
        size = p.stat().st_size
        if not size or size > MAX_SINGLE_BYTES:
            continue
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Toplam dosya siniri asildi.")
        files.append(p)
        if len(files) >= MAX_ITEMS:
            break
    if not files:
        LOG.error("SaveLivo extraction empty platform=%s engine=%s exit=%s detail=%s",
                  job.platform, engine, exit_status, failure_note)
        raise ValueError("Erisilebilir medya bulunamadi. Platform giris veya izin gerektiriyor olabilir.")
    if exit_status:
        LOG.warning("SaveLivo extraction partial platform=%s engine=%s exit=%s detail=%s",
                    job.platform, engine, exit_status, failure_note)
    job.count = len(files)
    if len(files) == 1:
        job.result = files[0]
    else:
        bundle = base / "savelivo-medya.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as output:
            for i, p in enumerate(files, 1):
                name = f"{i:02d}-{p.name[:90]}"
                output.write(p, arcname=name)
        job.result = bundle


async def _worker(job: Job, engine: str):
    global ACTIVE
    job.status = "working"
    try:
        await asyncio.to_thread(_run_command, job, engine)
        job.status = "ready"
        job.description = f"{job.count} medya dosyasi hazir."
    except Exception as exc:
        job.status = "failed"
        job.description = str(exc) if isinstance(exc, ValueError) else "Kaynak islenemedi. Baglanti veya platform kisiti olabilir."
        if not isinstance(exc, ValueError):
            LOG.error("SaveLivo worker failed platform=%s type=%s", job.platform, type(exc).__name__)
    finally:
        ACTIVE = max(0, ACTIVE - 1)


@router.get("/medya-yetenekleri")
async def capabilities():
    return {"durum": "hazir", "deneysel_platformlar_aktif": EXPERIMENTAL,
            "dosya_turleri": sorted(MEDIA_SUFFIXES),
            "platformlar": sorted(TEST_PROVIDERS), "sadece_yonetici": True, "dosya_siniri_mb": 30,
            "tek_islem_dosya_siniri": MAX_ITEMS, "not": "Listede olmasi indirilebilirlik garantisi degildir."}


@router.post("/hazirla")
async def begin(body: StartBody, request: Request):
    global ACTIVE
    require_owner(request)
    if not body.izin_var:
        raise HTTPException(403, "Yalnizca kendi icerigin veya izinli icerik indirilebilir.")
    _cleanup()
    url, (platform, engine) = await asyncio.to_thread(_validate_url, body.url)
    if ACTIVE >= 1 or sum(x.status in ("queued", "working") for x in JOBS.values()) >= MAX_TASKS:
        raise HTTPException(429, "Sunucu mesgul. Biraz sonra yeniden dene.")
    client = request.client.host if request.client else "unknown"
    attempts = RECENT.setdefault(client, [])
    if len(attempts) >= 3:
        raise HTTPException(429, "Bir dakika icinde en fazla 3 islem baslatabilirsin.")
    attempts.append(time.time())
    job = Job(id=token_urlsafe(20), url=url, platform=platform)
    JOBS[job.id] = job
    ACTIVE += 1
    asyncio.create_task(_worker(job, engine))
    return {"durum": "hazirlaniyor", "is_id": job.id, "platform": platform,
            "aciklama": "Yalnizca erisilebilir ve izinli medya kontrol ediliyor."}


@router.get("/isler/{job_id}")
async def state(job_id: str, request: Request):
    require_owner(request)
    _cleanup()
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Islem bulunamadi veya suresi doldu.")
    result = {"durum": job.status, "platform": job.platform, "aciklama": job.description}
    if job.status == "ready" and job.result and job.result.exists():
        result["secenekler"] = [{"format": "ZIP" if job.result.suffix == ".zip" else job.result.suffix[1:].upper(),
                                 "boyut_bayt": job.result.stat().st_size,
                                 "indir_url": f"/api/sonuc-dosyasi/{job.id}"}]
    return result


@router.get("/sonuc-dosyasi/{job_id}")
async def fetch_result(job_id: str, request: Request):
    require_owner(request)
    _cleanup()
    job = JOBS.get(job_id)
    if not job or job.status != "ready" or not job.result or not job.result.is_file():
        raise HTTPException(404, "Dosya bulunamadi veya suresi doldu.")
    suffix = job.result.suffix.lower()
    kind = "application/zip" if suffix == ".zip" else "application/octet-stream"
    filename = "savelivo-medya.zip" if suffix == ".zip" else "savelivo-" + job.result.name[:80]
    return FileResponse(job.result, media_type=kind, filename=filename,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
