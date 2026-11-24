"""Offline regression test for the public-page to album-browser route."""
from unittest import TestCase, main
from unittest.mock import patch
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import instagram_photos as m

COVER = "https://scontent.cdninstagram.com/cover.jpg"
OTHER = "https://scontent.cdninstagram.com/other.jpg"

class Response:
    status_code = 200
    headers = {"content-type": "text/html"}
    def __init__(self, html):
        self.html = html
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_bytes(self, **kwargs): yield self.html.encode()

class Client:
    def __init__(self, html): self.html = html
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def stream(self, *args, **kwargs): return Response(self.html)

class Routing(TestCase):
    def candidate(self, extra, shortcode="abcd"):
        html = (
            '<html><head><meta property="og:type" content="article">'
            '<meta property="og:site_name" content="Instagram">'
            '<meta property="og:url" content="https://www.instagram.com/p/abcd/">'
            f'<meta property="og:image" content="{COVER}">'
            + extra + '</head><body></body></html>'
        )
        candidates = []
        with patch.object(m.httpx, "Client", return_value=Client(html)), \
             patch.object(m, "_validate_cdn"), \
             patch.object(m, "_public_page_candidates", return_value=None):
            result = m._public_page_photo(shortcode, candidates)
        return result, candidates

    def test_missing_medium(self):
        self.assertEqual(self.candidate(""), (None, [COVER]))

    def test_multiple_previews(self):
        self.assertEqual(self.candidate(f'<meta property="og:image" content="{OTHER}">'), (None, [COVER]))

    def test_explicit_video(self):
        self.assertEqual(self.candidate('<meta name="medium" content="video">'), (None, []))

    def test_single_photo_unchanged(self):
        self.assertEqual(self.candidate('<meta name="medium" content="image"><meta name="twitter:title" content="Instagram photo">'), ([COVER], True), [])

    def test_wrong_post_rejected(self):
        with self.assertRaises(m.DownloadError):
            self.candidate("", shortcode="wxyz")

    def test_browser_selected_and_no_fallback_on_denial(self):
        import instagram_album_browser as browser
        def verified_page(code, candidates):
            candidates.append(COVER)
            return None
        with TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"ENABLE_INSTAGRAM_ALBUM_BROWSER":"true"}), \
             patch.object(m, "_public_page_photo", side_effect=verified_page), \
             patch.object(browser, "read_public_album", side_effect=browser.AlbumError("HTTP 429: test denial")) as reader, \
             patch.object(m, "_metadata") as old:
            with self.assertRaisesRegex(m.DownloadError, "HTTP 429"):
                m.download_post("https://www.instagram.com/p/abcd/", Path(directory))
            reader.assert_called_once()
            old.assert_not_called()

if __name__ == "__main__":
    main()
