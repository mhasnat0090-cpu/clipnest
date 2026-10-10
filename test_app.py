import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ValidateVideoUrlTests(unittest.TestCase):
    def test_rejects_private_ip_addresses(self) -> None:
        for url in ("http://127.0.0.1/video", "http://192.168.1.8/video", "http://[::1]/video"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                app.validate_video_url(url)

    def test_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "embedded usernames or passwords"):
            app.validate_video_url("https://user:password@example.com/video")

    @patch("app.socket.getaddrinfo")
    def test_accepts_public_https_url(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("8.8.8.8", 443)),
        ]
        self.assertEqual(app.validate_video_url("https://example.com/watch?v=1"), "https://example.com/watch?v=1")

    @patch("app.socket.getaddrinfo")
    def test_rejects_host_resolving_to_private_address(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [
            (2, 1, 6, "", ("10.0.0.2", 443)),
        ]
        with self.assertRaisesRegex(ValueError, "Private and local"):
            app.validate_video_url("https://example.com/watch?v=1")


class DownloadJobTests(unittest.TestCase):
    def test_quality_options_are_validated(self) -> None:
        for quality in ("best", "1080", "720", "480", "360"):
            self.assertEqual(app.validate_quality(quality), quality)
        with self.assertRaises(ValueError):
            app.validate_quality("2160")

    def test_queue_worker_runs_jobs_sequentially_with_requested_quality(self) -> None:
        original_jobs = app.JOBS.copy()
        original_queue = app.QUEUE.copy()
        original_active = app.ACTIVE_JOB_ID
        calls = []
        try:
            app.JOBS.clear()
            app.QUEUE.clear()
            app.JOBS.update({
                "first": {"id": "first", "status": "queued", "url": "https://example.com/1", "quality": "720", "updated_at": 0},
                "second": {"id": "second", "status": "queued", "url": "https://example.com/2", "quality": "480", "updated_at": 0},
            })
            app.QUEUE.extend(["first", "second"])
            with patch.object(app, "run_download", side_effect=lambda job_id, url, quality: calls.append((job_id, url, quality))):
                app.run_queue()
            self.assertEqual(calls, [
                ("first", "https://example.com/1", "720"),
                ("second", "https://example.com/2", "480"),
            ])
            self.assertEqual(app.QUEUE, [])
        finally:
            app.JOBS.clear()
            app.JOBS.update(original_jobs)
            app.QUEUE.clear()
            app.QUEUE.extend(original_queue)
            app.ACTIVE_JOB_ID = original_active

    def test_stop_resume_cancel_and_delete_state_rules(self) -> None:
        original_jobs = app.JOBS.copy()
        original_queue = app.QUEUE.copy()
        original_active = app.ACTIVE_JOB_ID

        class StubHandler:
            do_POST = app.VideoDownloaderHandler.do_POST
            do_GET = app.VideoDownloaderHandler.do_GET
            do_PATCH = app.VideoDownloaderHandler.do_PATCH
            do_DELETE = app.VideoDownloaderHandler.do_DELETE

            def __init__(self, method, path):
                self.command = method
                self.path = path
                self.status = None
                self.payload = None

            def send_json(self, status, data):
                self.status = status
                self.payload = data

        try:
            app.JOBS.clear()
            app.QUEUE.clear()
            app.ACTIVE_JOB_ID = None
            app.JOBS["queued"] = {"id": "queued", "status": "queued", "updated_at": 1}
            app.QUEUE.append("queued")
            handler = StubHandler("PATCH", "/api/jobs/queued/stop")
            handler.do_PATCH()
            self.assertEqual(handler.status, 200)
            self.assertEqual(app.JOBS["queued"]["status"], "paused")
            handler = StubHandler("PATCH", "/api/jobs/queued/resume")
            handler.do_PATCH()
            self.assertEqual(app.JOBS["queued"]["status"], "queued")
            handler = StubHandler("PATCH", "/api/jobs/queued/cancel")
            handler.do_PATCH()
            self.assertEqual(app.JOBS["queued"]["status"], "canceled")
            handler = StubHandler("DELETE", "/api/jobs/queued")
            handler.do_DELETE()
            self.assertEqual(handler.status, 200)
            self.assertNotIn("queued", app.JOBS)
        finally:
            app.JOBS.clear()
            app.JOBS.update(original_jobs)
            app.QUEUE.clear()
            app.QUEUE.extend(original_queue)
            app.ACTIVE_JOB_ID = original_active

    def test_explains_youtube_bot_verification_failure(self) -> None:
        message = app.clean_error(
            RuntimeError(
                "ERROR: [youtube] HqjPEwhiWcE: Sign in to confirm you’re not a bot. "
                "Use --cookies-from-browser or --cookies for the authentication."
            ),
            "https://youtube.com/watch?v=example",
        )
        self.assertIn("YouTube requires sign-in verification", message)
        self.assertIn("cookies.txt", message)

    def test_cookiefile_is_used_only_when_local_file_exists(self) -> None:
        job_id = "test-cookie-option"
        app.JOBS[job_id] = {"id": job_id, "status": "queued", "updated_at": 0}

        class CapturingDownloader:
            options = None

            def __init__(self, options):
                type(self).options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def download(self, _urls):
                template = Path(self.options["outtmpl"])
                template.parent.mkdir(parents=True, exist_ok=True)
                (template.parent / "sample.mp4").write_bytes(b"video")
                return 0

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                downloads = root / "downloads"
                cookies = root / "cookies.txt"
                cookies.write_text("# Netscape HTTP Cookie File\\n", encoding="utf-8")
                with patch.object(app, "DOWNLOADS", downloads), patch.object(app, "COOKIES_FILE", cookies), patch.object(app.yt_dlp, "YoutubeDL", CapturingDownloader):
                    app.run_download(job_id, "https://example.com/watch?v=1", "best")
                self.assertEqual(CapturingDownloader.options["cookiefile"], str(cookies))
                self.assertEqual(app.JOBS[job_id]["status"], "complete")
        finally:
            app.JOBS.pop(job_id, None)

    def test_download_job_records_finished_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            job_id = "test-job"
            app.JOBS[job_id] = {"id": job_id, "status": "queued", "updated_at": 0}

            class FakeDownloader:
                def __init__(self, options):
                    self.options = options

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def download(self, _urls):
                    template = Path(self.options["outtmpl"])
                    template.parent.mkdir(parents=True, exist_ok=True)
                    (template.parent / "sample.mp4").write_bytes(b"video")
                    return 0

            try:
                with patch.object(app, "DOWNLOADS", Path(temp_dir)), patch.object(app.yt_dlp, "YoutubeDL", FakeDownloader):
                    app.run_download(job_id, "https://example.com/watch?v=1", "best")

                self.assertEqual(app.JOBS[job_id]["status"], "complete")
                self.assertEqual(app.JOBS[job_id]["filename"], "sample.mp4")
                self.assertEqual(app.JOBS[job_id]["size"], 5)
            finally:
                app.JOBS.pop(job_id, None)

    def test_nonzero_ytdlp_exit_surfaces_clean_youtube_error(self) -> None:
        job_id = "test-youtube-error"
        app.JOBS[job_id] = {"id": job_id, "status": "queued", "updated_at": 0}

        class FailedDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def download(self, _urls):
                self.options["logger"].error(
                    "ERROR: [youtube] Sign in to confirm you’re not a bot. Use --cookies-from-browser."
                )
                return 1

        try:
            with patch.object(app.yt_dlp, "YoutubeDL", FailedDownloader):
                app.run_download(job_id, "https://youtube.com/watch?v=example", "best")
            self.assertEqual(app.JOBS[job_id]["status"], "error")
            self.assertIn("YouTube requires sign-in verification", app.JOBS[job_id]["error"])
        finally:
            app.JOBS.pop(job_id, None)


if __name__ == "__main__":
    unittest.main()
