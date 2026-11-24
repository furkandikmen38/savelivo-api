"""Experimental signed-out public photo-album reader. Disabled by default."""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

NEXT = 'button[aria-label="Next"], button[aria-label="İleri"]'
PREV = 'button[aria-label="Previous"], button[aria-label="Geri Dön"], button[aria-label="Back"]'


class AlbumError(ValueError):
    pass


def asset_key(url):
    try:
        p = urlsplit(url)
        if (p.scheme == 'https' and p.port in (None, 443) and not p.username
                and not p.password and not p.fragment and p.path
                and any(p.hostname and p.hostname.endswith('.' + root)
                        for root in ('cdninstagram.com', 'fbcdn.net'))):
            return p.path
    except (ValueError, TypeError):
        pass
    return None


SELECT = r'''button => {
  let root = button.parentElement, list = null;
  for (let i=0; root && i<10; i++, root=root.parentElement) {
    const lists=[...root.querySelectorAll('ul')].filter(x=>x.querySelector('div._aagv img'));
    if (lists.length>1) return null;
    if (lists.length===1) { list=lists[0]; break; }
  }
  if (!list) return null;
  const rect=e=>e.getBoundingClientRect();
  const area=r=>Math.max(0,r.right-r.left)*Math.max(0,r.bottom-r.top);
  const intersect=(a,b)=>({left:Math.max(a.left,b.left),right:Math.min(a.right,b.right),top:Math.max(a.top,b.top),bottom:Math.min(a.bottom,b.bottom)});
  const visible=e=>{
    const bounds=rect(e); let shown=intersect(bounds,{left:0,right:innerWidth,top:0,bottom:innerHeight});
    for(let p=e;p;p=p.parentElement){
      const s=getComputedStyle(p), r=rect(p);
      if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return 0;
      if(/hidden|clip|auto|scroll/.test(s.overflowX)){shown.left=Math.max(shown.left,r.left);shown.right=Math.min(shown.right,r.right);}
      if(/hidden|clip|auto|scroll/.test(s.overflowY)){shown.top=Math.max(shown.top,r.top);shown.bottom=Math.min(shown.bottom,r.bottom);}
    }
    return area(shown)/Math.max(1,area(bounds))>=0.55?area(shown):0;
  };
  if([...list.querySelectorAll('video')].some(v=>visible(v)>10000))return null;
  const images=[...list.querySelectorAll('div._aagv img')].filter(x=>x.complete&&x.naturalWidth)
    .map(x=>({url:x.currentSrc||x.src,area:visible(x)})).filter(x=>x.area>10000).sort((a,b)=>b.area-a.area);
  if(!images.length||(images[1]&&images[1].area>0.5*images[0].area))return null;
  return images[0].url;
}'''


def _controls(page, selector):
    return [b for b in page.locator('main').locator(selector).all()
            if b.is_visible() and b.is_enabled() and b.get_attribute('aria-disabled') != 'true']


def collect_album(page, shortcode, cover, max_items=8, seconds=35, denied=lambda: False):
    """Read a complete, distinct album via public visible controls, or fail closed."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,32}', shortcode) or not asset_key(cover):
        raise AlbumError('ALBUM_UNAVAILABLE: Invalid post or cover.')
    deadline = time.monotonic() + seconds
    result = []
    keys = set()
    old = None
    for index in range(max_items):
        stable = None
        since = 0
        while time.monotonic() < deadline:
            if denied():
                raise AlbumError('ACCESS_DENIED: Browser request denied or rate limited.')
            if page.is_closed():
                raise AlbumError('ALBUM_UNAVAILABLE: Browser page closed.')
            p = urlsplit(page.url)
            if (p.scheme != 'https' or p.hostname not in ('instagram.com', 'www.instagram.com')
                    or p.path.rstrip('/') != '/p/' + shortcode):
                raise AlbumError('ACCESS_DENIED: Public post no longer available.')
            nxt, prev = _controls(page, NEXT), _controls(page, PREV)
            if len(nxt) > 1 or len(prev) > 1:
                raise AlbumError('ALBUM_UNAVAILABLE: Ambiguous carousel.')
            anchor = (nxt or prev)
            url = anchor[0].evaluate(SELECT, timeout=1000) if anchor else None
            key = asset_key(url) if url else None
            valid = bool(key and key != old and ((index == 0 and nxt and not prev) or (index > 0 and prev)))
            state = (key, bool(nxt), bool(prev)) if valid else None
            now = time.monotonic()
            if state and state == stable and now - since >= 0.8:
                break
            if state != stable:
                stable, since = state, now
            page.wait_for_timeout(200)
        else:
            raise AlbumError('ALBUM_UNAVAILABLE: Visible photo or navigation timed out.')
        if key in keys or (index == 0 and key != asset_key(cover)):
            raise AlbumError('ALBUM_UNAVAILABLE: Album identity or order mismatch.')
        result.append(url)
        keys.add(key)
        old = key
        if denied():
            raise AlbumError('ACCESS_DENIED: Browser request denied or rate limited.')
        if not nxt:
            if len(result) < 2 or not prev:
                raise AlbumError('ALBUM_UNAVAILABLE: Album endpoint not confirmed.')
            return result
        if index == max_items - 1:
            raise AlbumError('ALBUM_UNAVAILABLE: Album exceeds supported limit.')
        nxt[0].click(timeout=2000)
    raise AlbumError('ALBUM_UNAVAILABLE: Album not complete.')


def _allowed_request(url):
    try:
        p = urlsplit(url)
        host = p.hostname or ''
        return (p.scheme == 'https' and p.port in (None, 443)
                and not p.username and not p.password
                and any(host == root or host.endswith('.' + root)
                        for root in ('instagram.com', 'cdninstagram.com', 'fbcdn.net')))
    except ValueError:
        return False


def read_public_album(shortcode, cover, max_items=8):
    """Use a fresh browser without login, credentials, proxies or retry loops."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,32}', shortcode) or not asset_key(cover):
        raise AlbumError('ALBUM_UNAVAILABLE: Invalid post or cover.')
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise AlbumError('ALBUM_UNAVAILABLE: Browser dependency is not installed.') from None
    blocked = [None]
    user_agent = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, timeout=8000)
            try:
                context = browser.new_context(locale='en-US', viewport={'width':1280,'height':1000},
                                              accept_downloads=False, service_workers='block')
                context.route('**/*', lambda route: route.continue_() if _allowed_request(route.request.url) else route.abort())
                page = context.new_page()
                page.set_default_timeout(2000)
                def response(r):
                    if _allowed_request(r.url):
                        if r.status == 429:
                            blocked[0] = 429
                        elif r.status in (401, 403) and (r.request.is_navigation_request() or r.request.resource_type == 'image'):
                            if blocked[0] != 429:
                                blocked[0] = r.status
                page.on('response', response)
                res = page.goto('https://www.instagram.com/p/' + shortcode + '/', wait_until='commit', timeout=12000)
                if res is None or res.status != 200:
                    raise AlbumError('ALBUM_UNAVAILABLE: Public page unavailable.')
                urls = collect_album(page, shortcode, cover, max_items=max_items,
                                     seconds=25, denied=lambda: bool(blocked[0]))
                user_agent = page.evaluate('navigator.userAgent')
                if blocked[0]:
                    raise AlbumError('ACCESS_DENIED: Browser request denied.')
                return urls, user_agent
            finally:
                browser.close()
    except Exception as exc:
        if blocked[0] == 429:
            raise AlbumError('HTTP 429: Instagram rate limited. stage=album_browser') from None
        if blocked[0] in (401, 403):
            raise AlbumError('ACCESS_DENIED: Instagram denied access. stage=album_browser') from None
        if isinstance(exc, AlbumError):
            raise
        raise AlbumError('ALBUM_UNAVAILABLE: Browser failed. type=' + type(exc).__name__) from None
