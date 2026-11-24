"""Offline regression tests for Instagram routing and job failure handling."""
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import media_jobs as jobs


class RoutingTests(unittest.TestCase):
    def test_photo_route_changes_without_changing_working_video_routes(self):
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        cases = {
            "https://www.instagram.com/p/AbCd_123/": ("Instagram", "instagram_photos"),
            "https://www.instagram.com/reel/AbCd_123/": ("Instagram", "video"),
            "https://www.tiktok.com/@example/video/1234567890": ("TikTok", "video"),
            "https://www.tiktok.com/@example/photo/1234567890": ("TikTok", "gallery"),
        }
        with patch.object(jobs.socket, "getaddrinfo", return_value=address):
            for url, expected in cases.items():
                with self.subTest(url=url):
                    self.assertEqual(jobs._validate_url(url)[1], expected)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / "job"
        self.base.mkdir()
        self.job = jobs.Job("offline-test", "https://www.instagram.com/p/AbCd_123/", "Instagram")
        self.directory_patch = patch.object(jobs.tempfile, "mkdtemp", return_value=str(self.base))
        self.cooldown_patch = patch.object(jobs, "INSTAGRAM_BLOCKED_UNTIL", 0.0)
        self.directory_patch.start()
        self.cooldown_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.directory_patch.stop)
        self.addCleanup(self.cooldown_patch.stop)

    def test_success_uses_isolated_adapter_and_preserves_jpeg_extension(self):
        seen = {}

        def run(args, **kwargs):
            seen.update(args=args, kwargs=kwargs)
            (Path(args[-1]) / "instagram-AbCd_123-01.jpg").write_bytes(b"\xff\xd8\xfftest\xff\xd9")
            return SimpleNamespace(returncode=0)

        with patch.dict(jobs.os.environ, {"SAVELIVO_TEST_KEY": "never-forward-this-test-value"}), \
                patch.object(jobs.subprocess, "run", side_effect=run) as command:
            jobs._run_command(self.job, "instagram_photos")
        self.assertEqual(command.call_count, 1)
        self.assertEqual(Path(seen["args"][1]).name, "instagram_photos.py")
        self.assertTrue(Path(seen["args"][1]).is_absolute())
        self.assertEqual(seen["args"][2], self.job.url)
        self.assertNotIn("SAVELIVO_TEST_KEY", seen["kwargs"]["env"])
        self.assertEqual(seen["kwargs"]["timeout"], 75)
        self.assertEqual(self.job.count, 1)
        self.assertEqual(self.job.result.suffix, ".jpg")

    def test_nonzero_exit_never_serves_an_incomplete_carousel(self):
        def run(args, **kwargs):
            (Path(args[-1]) / "first.jpg").write_bytes(b"\xff\xd8\xfftest")
            kwargs["stderr"].write(b"ERROR: second media download failed\n")
            return SimpleNamespace(returncode=1)

        with patch.object(jobs.subprocess, "run", side_effect=run) as command:
            with self.assertRaises(ValueError):
                jobs._run_command(self.job, "instagram_photos")
        self.assertEqual(command.call_count, 1)
        self.assertIsNone(self.job.result)
        self.assertFalse((self.base / "indirilenler").exists())

    def test_photo_carousel_returns_only_one_image_without_zip(self):
        def run(args, **kwargs):
            for index in (3, 1, 2):
                (Path(args[-1]) / f"instagram-{index:02d}.jpg").write_bytes(b"\xff\xd8\xfftest")
            return SimpleNamespace(returncode=0)

        with patch.object(jobs.subprocess, "run", side_effect=run):
            jobs._run_command(self.job, "instagram_photos")
        self.assertEqual(self.job.count, 1)
        self.assertEqual(self.job.result.name, "instagram-01.jpg")
        self.assertEqual(self.job.result.suffix, ".jpg")
        self.assertNotEqual(self.job.result.suffix, ".zip")

    def test_gallery_command_requests_only_the_first_item(self):
        seen = {}
        def run(args, **kwargs):
            seen["args"] = args
            (Path(args[-1]) / "first.jpg").write_bytes(b"\xff\xd8\xfffirst")
            (Path(args[-1]) / "second.jpg").write_bytes(b"\xff\xd8\xffsecond")
            return SimpleNamespace(returncode=0)

        with patch.object(jobs.subprocess, "run", side_effect=run):
            jobs._run_command(self.job, "gallery")
        range_index = seen["args"].index("--range")
        self.assertEqual(seen["args"][range_index + 1], "1-1")
        self.assertEqual(self.job.count, 1)
        self.assertEqual(self.job.result.name, "first.jpg")
        self.assertNotEqual(self.job.result.suffix, ".zip")

    def test_oversized_photo_has_an_actionable_message(self):
        def run(args, **kwargs):
            kwargs["stderr"].write(b"MEDIA_TOO_LARGE: File limit exceeded\n")
            return SimpleNamespace(returncode=1)

        with patch.object(jobs.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "30 MB, toplam 100 MB"):
                jobs._run_command(self.job, "instagram_photos")

    def test_429_stops_and_sets_cooldown_even_with_a_partial_file(self):
        def run(args, **kwargs):
            (Path(args[-1]) / "first.jpg").write_bytes(b"partial")
            kwargs["stderr"].write(b"ERROR: HTTP 429 Too Many Requests\n")
            return SimpleNamespace(returncode=1)

        with patch.object(jobs.subprocess, "run", side_effect=run) as command:
            with self.assertRaisesRegex(ValueError, "429"):
                jobs._run_command(self.job, "instagram_photos")
        self.assertEqual(command.call_count, 1)
        self.assertGreater(jobs._instagram_cooldown_remaining(), 895)
        self.assertIsNone(self.job.result)
        self.assertFalse((self.base / "indirilenler").exists())

    def test_429_written_before_timeout_is_not_lost(self):
        def run(args, **kwargs):
            kwargs["stderr"].write(b"ERROR: 429 Too Many Requests\n")
            raise subprocess.TimeoutExpired(args, 75)

        with patch.object(jobs.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "429"):
                jobs._run_command(self.job, "instagram_photos")
        self.assertGreater(jobs._instagram_cooldown_remaining(), 895)

    def test_plain_timeout_fails_without_claiming_rate_limiting(self):
        with patch.object(jobs.subprocess, "run", side_effect=subprocess.TimeoutExpired("offline", 75)):
            with self.assertRaisesRegex(ValueError, "süresi doldu"):
                jobs._run_command(self.job, "instagram_photos")
        self.assertEqual(jobs._instagram_cooldown_remaining(), 0)
        self.assertIsNone(self.job.result)


class EndpointTests(unittest.TestCase):
    def test_owner_key_and_cooldown_prevent_extractor_calls(self):
        app = FastAPI()
        app.include_router(jobs.router)
        body = {"url": "https://www.instagram.com/p/AbCd_123/", "izin_var": True}
        key = "offline-owner-test-key-not-a-real-secret"
        with patch.object(jobs, "EXPERIMENTAL", True), \
                patch.object(jobs, "OWNER_KEY", key), \
                patch.object(jobs, "INSTAGRAM_BLOCKED_UNTIL", time.monotonic() + 900), \
                patch.object(jobs, "_validate_url", return_value=(body["url"], ("Instagram", "instagram_photos"))), \
                patch.object(jobs.subprocess, "run") as command, TestClient(app) as client:
            unauthorized = client.post("/api/hazirla", json=body)
            self.assertEqual(unauthorized.status_code, 403)
            paused = client.post("/api/hazirla", json=body, headers={"X-SaveLivo-Test-Key": key})
            self.assertEqual(paused.status_code, 429)
            self.assertGreater(int(paused.headers["Retry-After"]), 895)
            self.assertIn("koruma aralığı", paused.json()["detail"])
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
