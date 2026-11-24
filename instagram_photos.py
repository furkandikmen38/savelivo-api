"""Bounded anonymous Instagram post downloads using Instaloader 4.16 metadata."""
from __future__ import annotations

import ipaddress
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import requests
from instaloader import InstaloaderContext, Post

MAX_ITEMS = 8
MAX_SINGLE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
CDN_DOMAINS = ("cdninstagram.com", "fbcdn.net")


class DownloadError(ValueError):
    """Safe diagnostic text that can be printed without upstream response bodies."""


def _http_error(status: int, stage: str, location: str = "") -> DownloadError:
    if status == 429:
        return DownloadError(f"HTTP 429: Instagram istegi sinirladi. stage={stage}")
    if 300 <= status < 400:
        try:
            login_redirect = "/accounts/login" in urlsplit(location).path.lower()
        except ValueError:
            login_redirect = False
        code = "LOGIN_REQUIRED" if login_redirect else "HTTP_REDIRECT"
    elif status == 401:
        code = "LOGIN_REQUIRED"
    elif status == 403:
        code = "ACCESS_DENIED"
    elif status == 404:
        code = "NOT_FOUND"
    elif 500 <= status < 600:
        code = "UPSTREAM_ERROR"
    else:
        code = "HTTP_ERROR"
    return DownloadError(f"{code}: HTTP {status} stage={stage}")


def _exception_error(exc: Exception, stage: str) -> DownloadError:
    """Report safe exception categories, never raw messages, URLs or headers."""
    chain = []
    current = exc
    while current is not None and len(chain) < 8 and all(current is not e for e in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    kind = re.sub(r"[^A-Za-z0-9_]", "_", type(exc).__name__)[:64]
    for item in chain:
        if isinstance(item, DownloadError):
            return item
        response = getattr(item, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 300 <= status <= 599:
            return _http_error(status, stage, response.headers.get("Location", ""))
        # Some upstream exceptions wrap HTTP status in text, not response objects.
        note = str(item)
        if re.search(r"\b429\b|too many requests", note, re.IGNORECASE):
            return _http_error(429, stage)
        match = re.search(r"\b(?:HTTP\s+|status(?:\s+code)?[\s:=]+)([345]\d\d)\b|\b(401|403|404|500|502|503|504)\s+(?:Unauthorized|Forbidden|Not Found|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout)\b", note, re.IGNORECASE)
        if match:
            return _http_error(int(match.group(1) or match.group(2)), stage)
        name = type(item).__name__
        if name == "TooManyRequestsException":
            return _http_error(429, stage)
        if name in ("LoginRequiredException", "PrivateProfileNotFollowedException") or re.search(r"redirect(?:ed)? to (?:the )?login page|login required|login is required", note, re.IGNORECASE):
            return DownloadError(f"LOGIN_REQUIRED: stage={stage} type={kind}")
        if name == "QueryReturnedForbiddenException":
            return _http_error(403, stage)
        if name in ("QueryReturnedNotFoundException", "PostNotExistsException"):
            return _http_error(404, stage)
    if any(isinstance(e, (requests.exceptions.Timeout, httpx.TimeoutException, TimeoutError)) for e in chain):
        code = "NETWORK_TIMEOUT"
    elif any(isinstance(e, (requests.exceptions.SSLError,)) for e in chain):
        code = "TLS_ERROR"
    elif any(type(e).__name__ in ("JSONDecodeError", "BadResponseException") for e in chain):
        code = "RESPONSE_FORMAT_ERROR"
    elif any(isinstance(e, (requests.exceptions.ConnectionError, httpx.NetworkError, ConnectionError, OSError)) for e in chain):
        code = "NETWORK_ERROR"
    elif any(isinstance(e, (AttributeError, TypeError, NameError, KeyError, IndexError, ImportError)) for e in chain):
        code = "CODE_OR_SCHEMA_ERROR"
    else:
        code = "UNCLASSIFIED_ERROR"
    return DownloadError(f"{code}: stage={stage} type={kind}")


def _check_response(response, **_kwargs):
    if response.status_code != 200:
        raise _http_error(response.status_code, "metadata", response.headers.get("Location", ""))
    return response


class _PublicContext(InstaloaderContext):
    def get_anonymous_session(self):
        session = super().get_anonymous_session()
        session.trust_env = False
        session.hooks["response"].append(_check_response)
        return session

    def _csrf_token(self):
        # Pinned 4.16 integration: use the first page's anonymous CSRF cookie.
        # The upstream fallback makes an unchecked extra GET; never make it here.
        token = next((c.value for c in self._session.cookies
                      if c.name == "csrftoken" and c.value), None)
        if not token:
            raise DownloadError("LOGIN_REQUIRED: Anonim Instagram oturumu acilamadi.")
        return token


def _shortcode(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        valid = (parsed.scheme == "https" and
                 parsed.hostname in ("instagram.com", "www.instagram.com") and
                 parsed.port in (None, 443) and not parsed.username and
                 not parsed.password and not parsed.fragment)
        match = re.fullmatch(r"/p/([A-Za-z0-9_-]{4,32})/?", parsed.path)
    except ValueError:
        valid, match = False, None
    if not valid or not match:
        raise DownloadError("INVALID_URL: Tek bir Instagram gonderisi gerekli.")
    return match[1]


def _media(post: Post) -> list[tuple[bool, str]]:
    if post.typename == "GraphSidecar":
        entries = [(node.is_video, node.video_url if node.is_video else node.display_url)
                   for node in post.get_sidecar_nodes(start=0, end=MAX_ITEMS - 1)]
    elif post.typename == "GraphImage":
        entries = [(False, post.url)]
    elif post.typename == "GraphVideo":
        entries = [(True, post.video_url)]
    else:
        raise DownloadError("UNSUPPORTED_MEDIA: Gonderi turu desteklenmiyor.")
    if not entries or any(not isinstance(url, str) or not url for _, url in entries):
        raise DownloadError("EMPTY_MEDIA: Gonderide erisilebilir medya yok.")
    return entries


def _metadata(shortcode: str) -> tuple[list[tuple[bool, str]], str]:
    context = _PublicContext(quiet=True, max_connection_attempts=1,
                             request_timeout=12, iphone_support=False,
                             fatal_status_codes=[301, 302, 303, 307, 308, 400, 401, 403, 429])
    try:
        post = Post.from_shortcode(context, shortcode)
        return _media(post), context.user_agent
    except DownloadError:
        raise
    except Exception as exc:
        raise _exception_error(exc, "metadata") from None
    finally:
        context.close()


def _validate_cdn(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        valid = (parsed.scheme == "https" and parsed.port in (None, 443) and
                 not parsed.username and not parsed.password and not parsed.fragment and
                 any(host == root or host.endswith("." + root) for root in CDN_DOMAINS))
        if not valid:
            raise ValueError
        addresses = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError
    except (ValueError, OSError):
        raise DownloadError("UNSUPPORTED_MEDIA: Medya sunucusu dogrulanamadi.") from None


def _extension(prefix: bytes, content_type: str, video: bool) -> str:
    if prefix.startswith(b"\xff\xd8\xff"):
        extension, mime = "jpg", "image/jpeg"
    elif prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        extension, mime = "png", "image/png"
    elif prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        extension, mime = "webp", "image/webp"
    elif prefix[4:8] == b"ftyp" and any(b in prefix[8:32] for b in (b"avif", b"avis")):
        extension, mime = "avif", "image/avif"
    elif prefix[4:8] == b"ftyp" and video:
        extension, mime = "mp4", "video/mp4"
    else:
        raise DownloadError("UNSUPPORTED_MEDIA: Dosya gecerli bir fotograf veya MP4 degil.")
    content_type = content_type.split(";", 1)[0].strip().lower()
    if video != (extension == "mp4") or content_type not in (mime, "application/octet-stream"):
        raise DownloadError("UNSUPPORTED_MEDIA: Dosya turu beklenen medya ile uyusmuyor.")
    return extension


def download_post(url: str, target: Path) -> int:
    """Download up to eight ordered media files; leave no results on any failure."""
    entries, user_agent = _metadata(_shortcode(url))
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    ready: list[tuple[Path, Path]] = []
    total = 0
    try:
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False,
                          headers={"User-Agent": user_agent, "Referer": "https://www.instagram.com/"}) as client:
            for index, (video, media_url) in enumerate(entries, 1):
                _validate_cdn(media_url)
                with client.stream("GET", media_url) as response:
                    if response.status_code != 200:
                        raise _http_error(response.status_code, "cdn", response.headers.get("Location", ""))
                    length = int(response.headers.get("content-length", "0"))
                    if length > MAX_SINGLE_BYTES or total + length > MAX_TOTAL_BYTES:
                        raise DownloadError("MEDIA_TOO_LARGE: Dosya boyutu siniri asildi.")
                    chunks = response.iter_bytes(chunk_size=65536)
                    first = next(chunks, b"")
                    extension = _extension(first, response.headers.get("content-type", ""), video)
                    final = target / f"instagram-{index:02d}.{extension}"
                    partial = final.with_suffix(final.suffix + ".part")
                    if final.exists():
                        raise DownloadError("TARGET_EXISTS: Hedef dosya zaten var.")
                    size = 0
                    with partial.open("xb") as output:
                        created.append(partial)
                        chunk = first
                        while chunk:
                            size += len(chunk)
                            total += len(chunk)
                            if size > MAX_SINGLE_BYTES or total > MAX_TOTAL_BYTES:
                                raise DownloadError("MEDIA_TOO_LARGE: Dosya boyutu siniri asildi.")
                            output.write(chunk)
                            chunk = next(chunks, b"")
                    if not size or (length > 0 and size != length):
                        raise DownloadError("EMPTY_MEDIA: Medya dosyasi eksik indi.")
                    ready.append((partial, final))
        for partial, final in ready:
            partial.rename(final)
            created.append(final)
        return len(ready)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 2:
            raise DownloadError("INVALID_ARGUMENTS: Gonderi ve hedef klasor gerekli.")
        download_post(argv[0], Path(argv[1]))
        return 0
    except DownloadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"ERROR: {_exception_error(exc, 'worker')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
