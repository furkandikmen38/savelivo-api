import contextlib
import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import requests
import instaloader.instaloadercontext as context_module

import instagram_photos as adapter


JPEG = b"\xff\xd8\xff" + b"fixture-image-data" * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image-data" * 4
MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x00\x00isommp42" + b"media"
URL = "https://www.instagram.com/p/AbCdEf12345/"
CDN = "https://scontent.cdninstagram.com/"


def image_node(index=1):
    return {"media_type": 1, "image_versions2": {"candidates": [{"url": CDN + f"{index}.jpg"}]}}


def metadata_response(media_type=1, carousel=None):
    item = {"media_type": media_type, "code": "AbCdEf12345", "pk": "123456",
            "taken_at": 1700000000, "user": {"pk": "42", "username": "fixture"},
            "image_versions2": {"candidates": [{"url": CDN + "1.jpg"}]}}
    if carousel is not None:
        item["carousel_media"] = carousel
    return {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [item]}}}


class MetadataTests(unittest.TestCase):
    def test_real_library_parses_anonymous_single_photo(self):
        with patch.object(adapter._PublicContext, "doc_id_graphql_query",
                          return_value=metadata_response()) as query:
            media, _ = adapter._metadata("AbCdEf12345")
        self.assertEqual(media, [(False, CDN + "1.jpg")])
        self.assertEqual(query.call_count, 1)
        self.assertTrue(query.call_args.kwargs["web_api"])

    def test_real_library_returns_only_first_carousel_item(self):
        nodes = [image_node(i) for i in range(1, 10)]
        nodes[1] = {"media_type": 2, "video_versions": [{"url": CDN + "2.mp4"}],
                    "image_versions2": {"candidates": [{"url": CDN + "thumbnail.jpg"}]}}
        with patch.object(adapter._PublicContext, "doc_id_graphql_query",
                          return_value=metadata_response(8, nodes)) as query:
            media, _ = adapter._metadata("AbCdEf12345")
        self.assertEqual(adapter.MAX_ITEMS, 1)
        self.assertEqual(media, [(False, CDN + "1.jpg")])
        self.assertEqual(query.call_count, 1)

    def test_no_additional_homepage_request_when_csrf_missing(self):
        context = adapter._PublicContext(quiet=True)
        try:
            with patch.object(context._session, "get") as get:
                with self.assertRaisesRegex(adapter.DownloadError, "LOGIN_REQUIRED"):
                    context._csrf_token()
                get.assert_not_called()
            context._session.cookies.set("csrftoken", "anonymous-csrf", domain=".instagram.com")
            self.assertEqual(context._csrf_token(), "anonymous-csrf")
        finally:
            context.close()

    def test_anonymous_session_has_no_authentication_and_stops_429(self):
        context = adapter._PublicContext(quiet=True)
        try:
            self.assertFalse(context.is_logged_in)
            self.assertFalse(context._session.trust_env)
            self.assertFalse(context._session.cookies.get("sessionid"))
            response = requests.Response()
            response.status_code = 429
            with self.assertRaisesRegex(adapter.DownloadError, "HTTP 429"):
                for hook in context._session.hooks["response"]:
                    hook(response)
        finally:
            context.close()

    def test_raw_library_errors_are_not_exposed(self):
        from instaloader.exceptions import AbortDownloadException
        with patch.object(adapter.Post, "from_shortcode", side_effect=AbortDownloadException(
                "HTTP 429 private response https://example.test/?token=secret")):
            with self.assertRaises(adapter.DownloadError) as result:
                adapter._metadata("AbCdEf12345")
        self.assertIn("429", str(result.exception))
        self.assertNotIn("secret", str(result.exception))
        self.assertNotIn("example", str(result.exception))

    def test_real_graphql_429_and_redirect_stop_without_retry_or_followup(self):
        original_copy = context_module.copy_session

        def tokens(context):
            context._session.cookies.set("csrftoken", "anonymous-csrf", domain=".instagram.com")
            return {"lsd": "anonymous-lsd", "av": "0"}

        for status in (429, 302):
            with self.subTest(status=status):
                sessions = []
                response = requests.Response()
                response.status_code = status
                response.reason = "Too Many Requests" if status == 429 else "Found"
                response._content = b""
                response.headers["location"] = "https://www.instagram.com/challenge/"

                def copy_session(session, timeout):
                    copied = original_copy(session, timeout)
                    copied.post = Mock(return_value=response)
                    copied.get = Mock(side_effect=AssertionError("Unexpected follow-up request"))
                    sessions.append(copied)
                    return copied

                with patch.object(adapter._PublicContext, "_web_api_tokens", tokens), \
                     patch.object(context_module, "copy_session", side_effect=copy_session), \
                     patch.object(adapter._PublicContext, "do_sleep"):
                    with self.assertRaisesRegex(adapter.DownloadError, "429" if status == 429 else "EMPTY_MEDIA"):
                        adapter._metadata("AbCdEf12345")
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0].post.call_count, 1)
                sessions[0].get.assert_not_called()

    def test_url_requires_individual_canonical_post(self):
        self.assertEqual(adapter._shortcode(URL + "?igsh=tracking"), "AbCdEf12345")
        for url in ("http://www.instagram.com/p/AbCdEf12345/", "https://instagram.com/profile/",
                    "https://instagram.com/stories/profile/123/", "https://instagram.com.evil.test/p/AbCdEf12345/",
                    "https://user:pass@instagram.com/p/AbCdEf12345/", URL + "#fragment"):
            with self.subTest(url=url), self.assertRaises(adapter.DownloadError):
                adapter._shortcode(url)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.target = Path(self.temporary.name) / "media"
        self.target.mkdir()
        self.dns = patch.object(adapter.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.addCleanup(self.temporary.cleanup)

    @contextlib.contextmanager
    def setup_download(self, entries, handler):
        real_client = httpx.Client
        seen = []

        def transport(request):
            seen.append(str(request.url))
            return handler(request)

        def make_client(**kwargs):
            self.assertFalse(kwargs["follow_redirects"])
            self.assertFalse(kwargs["trust_env"])
            return real_client(transport=httpx.MockTransport(transport), **kwargs)

        with patch.object(adapter, "_metadata", return_value=(entries, "SaveLivo-test")), \
             patch.object(adapter.httpx, "Client", side_effect=make_client):
            yield seen

    def assert_empty(self):
        self.assertEqual(list(self.target.iterdir()), [])

    def test_multi_media_post_downloads_only_first_file(self):
        entries = [(False, CDN + "1.jpg"), (True, CDN + "2.mp4")]
        def response(request):
            return httpx.Response(200, content=MP4 if request.url.path.endswith("mp4") else JPEG,
                                  headers={"content-type": "video/mp4" if request.url.path.endswith("mp4") else "image/jpeg"})
        with self.setup_download(entries, response) as seen:
            self.assertEqual(adapter.download_post(URL, self.target), 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual((self.target / "instagram-01.jpg").read_bytes(), JPEG)
        self.assertEqual([p.name for p in self.target.iterdir()], ["instagram-01.jpg"])

    def test_429_stops_before_next_media_and_discards_prior_files(self):
        entries = [(False, CDN + f"{i}.jpg") for i in range(1, 4)]
        def response(request):
            if request.url.path == "/2.jpg":
                return httpx.Response(429, text="private error body")
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        with patch.object(adapter, "MAX_ITEMS", 3), self.setup_download(entries, response) as seen:
            with self.assertRaisesRegex(adapter.DownloadError, "HTTP 429"):
                adapter.download_post(URL, self.target)
        self.assertEqual(len(seen), 2)
        self.assert_empty()

    def test_html_cannot_be_saved_even_with_image_content_type(self):
        with self.setup_download([(False, CDN + "1.jpg")], lambda _: httpx.Response(
                200, content=b"<html>Login required</html>", headers={"content-type": "image/jpeg"})):
            with self.assertRaisesRegex(adapter.DownloadError, "UNSUPPORTED_MEDIA"):
                adapter.download_post(URL, self.target)
        self.assert_empty()

    def test_redirects_are_not_followed(self):
        with self.setup_download([(False, CDN + "1.jpg")], lambda _: httpx.Response(
                302, headers={"location": "http://127.0.0.1/private"})) as seen:
            with self.assertRaises(adapter.DownloadError):
                adapter.download_post(URL, self.target)
        self.assertEqual(len(seen), 1)
        self.assert_empty()

    def test_private_dns_and_unapproved_domains_are_rejected(self):
        for url in ("https://scontent.cdninstagram.com.evil.test/a.jpg", "https://127.0.0.1/a.jpg",
                    "https://user:pass@scontent.cdninstagram.com/a.jpg", CDN + "a.jpg#x"):
            with self.subTest(url=url), self.assertRaises(adapter.DownloadError):
                adapter._validate_cdn(url)
        with patch.object(adapter.socket, "getaddrinfo", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))]):
            with self.assertRaises(adapter.DownloadError):
                adapter._validate_cdn(CDN + "a.jpg")

    def test_stream_size_limit_without_content_length_removes_partial(self):
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                yield JPEG
                yield b"x" * 70000
        with patch.object(adapter, "MAX_SINGLE_BYTES", 66000), self.setup_download(
                [(False, CDN + "1.jpg")], lambda _: httpx.Response(
                    200, stream=Body(), headers={"content-type": "image/jpeg"})):
            with self.assertRaisesRegex(adapter.DownloadError, "MEDIA_TOO_LARGE"):
                adapter.download_post(URL, self.target)
        self.assert_empty()

    def test_total_size_limit_discards_previous_media(self):
        entries = [(False, CDN + f"{i}.jpg") for i in (1, 2)]
        with patch.object(adapter, "MAX_TOTAL_BYTES", len(JPEG) + 1), patch.object(adapter, "MAX_ITEMS", 2), self.setup_download(
                entries, lambda _: httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})):
            with self.assertRaisesRegex(adapter.DownloadError, "MEDIA_TOO_LARGE"):
                adapter.download_post(URL, self.target)
        self.assert_empty()

    def test_network_failure_discards_previous_media(self):
        def response(request):
            if request.url.path == "/2.jpg":
                raise httpx.ReadTimeout("upstream disconnected", request=request)
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        with patch.object(adapter, "MAX_ITEMS", 2), self.setup_download(
                [(False, CDN + "1.jpg"), (False, CDN + "2.jpg")], response):
            with self.assertRaises(httpx.ReadTimeout):
                adapter.download_post(URL, self.target)
        self.assert_empty()

    def test_cli_never_prints_raw_error_or_credentials(self):
        with patch.object(adapter, "download_post", side_effect=ValueError("token=SECRET")):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(adapter.main([URL, str(self.target)]), 1)
        self.assertNotIn("SECRET", output.getvalue())
        self.assertIn("EMPTY_MEDIA", output.getvalue())


if __name__ == "__main__":
    unittest.main()
