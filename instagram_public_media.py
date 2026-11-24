import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit


def _public_asset_key(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        if (p.scheme != 'https' or p.port not in (None, 443) or p.username or p.password
                or p.fragment or not any(host.endswith('.' + d) for d in ('cdninstagram.com', 'fbcdn.net'))
                or not re.fullmatch(r'/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp|avif)', p.path, re.I)):
            return None
        return p.path
    except (ValueError, TypeError, AttributeError):
        return None


def _public_rank(url, width=0):
    # Read the supplied URL's size hints; never alter its signature or query.
    transform = parse_qs(urlsplit(url).query).get('stp', [''])[0]
    cropped = bool(re.search(r'(?:^|_)c\d+\.', transform))
    size = re.search(r'(?:^|_)s(\d+)x(\d+)(?:_|$)', transform)
    return (not cropped, bool(width or size), max(width, int(size[1]) if size else 0)) if cropped or size else (True, True, max(width, 10000 if transform else 0))


class _PublicMediaCandidates(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images = []
        self.documents = []
        self.script = False
        self.script_size = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'script':
            self.script = (a.get('type') or '').lower() in ('application/json', 'application/ld+json')
        if tag == 'img' or (tag == 'link' and (a.get('as') or '').lower() == 'image'):
            url = a.get('src') or a.get('href')
            width = a.get('width') or ''
            if url:
                self.images.append((url, int(width) if width.isdigit() and len(width) < 6 else 0))
            for value in (a.get('srcset'), a.get('imagesrcset')):
                for item in (value or '').split(','):
                    bits = item.split()
                    if bits:
                        w = re.fullmatch(r'(\d{1,5})w', bits[1]) if len(bits) == 2 else None
                        self.images.append((bits[0], int(w[1]) if w else 0))

    def handle_endtag(self, tag):
        if tag == 'script':
            self.script = False

    def handle_data(self, data):
        if self.script and data.strip() and self.script_size + len(data) <= 8 * 1024 * 1024:
            self.script_size += len(data)
            try:
                self.documents.append(json.loads(data))
            except (ValueError, RecursionError):
                pass


def _public_node_images(node):
    choices = []
    versions = node.get('image_versions2')
    versions = versions.get('candidates', []) if isinstance(versions, dict) else []
    for values in (versions, node.get('display_resources', [])):
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            url = item.get('url') or item.get('src')
            width = item.get('width', item.get('config_width', 0))
            if _public_asset_key(url):
                choices.append((url, width if type(width) is int and 0 < width < 100000 else 0))
    for key in ('display_url', 'display_src'):
        url = node.get(key)
        if _public_asset_key(url):
            choices.append((url, 0))
    return choices


def _public_page_candidates(html, shortcode, cover, single, max_items):
    """Use only exact URLs in a public response; keep other posts out of the result."""
    cover_key = _public_asset_key(cover)
    if not cover_key:
        return None
    parser = _PublicMediaCandidates()
    parser.feed(html)
    parser.close()
    same = [(cover, 0)] + [(u, w) for u, w in parser.images if _public_asset_key(u) == cover_key]
    stack = list(parser.documents)
    visited = 0
    albums = []
    while stack and visited < 100000:
        node = stack.pop()
        visited += 1
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        stack.extend(node.values())
        if node.get('shortcode') != shortcode and node.get('code') != shortcode:
            continue
        if single:
            same.extend((u, w) for u, w in _public_node_images(node) if _public_asset_key(u) == cover_key)
            continue
        # A typed photo child may use a generic Relay typename. Its explicit
        # media_type is authoritative unless other fields identify a video.
        items = node.get('carousel_media')
        count = node.get('carousel_media_count')
        if not isinstance(items, list):
            side = node.get('edge_sidecar_to_children')
            if not isinstance(side, dict) or not isinstance(side.get('edges'), list):
                continue
            page_info = side.get('page_info')
            if isinstance(page_info, dict) and (page_info.get('has_next_page') is not False
                    or page_info.get('has_previous_page') is True):
                continue
            items = [e.get('node') if isinstance(e, dict) else None for e in side['edges']]
            count = side.get('count')
            if count is None and isinstance(page_info, dict) and page_info.get('has_next_page') is False and page_info.get('has_previous_page') is False:
                count = len(items)
        if isinstance(count, str) and re.fullmatch(r'[1-9][0-9]{0,4}', count):
            count = int(count)
        if type(count) is not int or count != len(items) or not 2 <= count <= max_items:
            continue
        chosen = []
        for item in items:
            if not isinstance(item, dict):
                break
            media_type = item.get('media_type')
            typename = item.get('__typename')
            if (item.get('is_video') is True or item.get('video_url') or item.get('video_versions')
                    or (isinstance(typename, str) and 'video' in typename.lower())
                    or (media_type is not None and (type(media_type) is not int or media_type != 1))
                    or (media_type is None and typename not in (None, 'GraphImage', 'XDTGraphImage'))):
                break
            candidates = _public_node_images(item)
            if not candidates:
                break
            chosen.append(max(candidates, key=lambda t: _public_rank(*t))[0])
        if (len(chosen) == count and _public_asset_key(chosen[0]) == cover_key
                and len({_public_asset_key(u) for u in chosen}) == count):
            albums.append(chosen)
    if single:
        best = max(same, key=lambda t: _public_rank(*t))[0]
        return [best]
    if albums and all([_public_asset_key(u) for u in a] == [_public_asset_key(u) for u in albums[0]] for a in albums):
        return albums[0]
    return None
