import ipaddress
import json
import mimetypes
import shutil
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yt_dlp


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DOWNLOADS = ROOT / "downloads"
COOKIES_FILE = ROOT / "cookies.txt"
MAX_REQUEST_BYTES = 16_384
MAX_QUEUED_DOWNLOADS = 50
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
JOB_TTL_SECONDS = 2 * 60 * 60
QUALITY_HEIGHTS = {"best": None, "1080": 1080, "720": 720, "480": 480, "360": 360}
JOBS: dict[str, dict[str, object]] = {}
JOBS_LOCK = threading.Lock()
QUEUE: list[str] = []
QUEUE_WORKER_RUNNING = False
ACTIVE_JOB_ID: str | None = None
QUEUE_CHANGED = threading.Condition(JOBS_LOCK)


class DownloadLogger:
    def __init__(self) -> None:
        self.error_message = ""

    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, message: str) -> None:
        self.error_message = message


def validate_video_url(raw_url: object) -> str:
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 4096:
        raise ValueError("Enter a valid video URL.")
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use a complete http or https video URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded usernames or passwords are not accepted.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("The URL has an invalid port.") from error

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("Local network URLs are not accepted.")
    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as error:
            raise ValueError("The video host could not be resolved.") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Private and local network addresses are not accepted.")
    return raw_url.strip()


def validate_quality(raw_quality: object) -> str:
    if not isinstance(raw_quality, str) or raw_quality not in QUALITY_HEIGHTS:
        raise ValueError("Choose a supported video quality.")
    return raw_quality


def clean_error(error: BaseException, original_url: str) -> str:
    message = str(error).replace(original_url, "video URL").strip()
    normalized = message.casefold()
    if "sign in to confirm" in normalized and "bot" in normalized:
        return (
            "YouTube requires sign-in verification for this video. Add a valid Netscape-format cookies.txt "
            "beside app.py and restart Clipnest, or try another video."
        )
    if not message:
        return "The video could not be downloaded. Check the URL and try again."
    return message[:280]


def update_progress(job_id: str, status: dict[str, object]) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(status)
            job["updated_at"] = time.time()


class DownloadStopped(Exception):
    pass


def run_download(job_id: str, video_url: str, quality: str) -> None:
    job_directory = DOWNLOADS / job_id
    job_directory.mkdir(parents=True, exist_ok=True)

    def progress_hook(event: dict[str, object]) -> None:
        with JOBS_LOCK:
            current = JOBS.get(job_id)
            canceled = current is None or bool(current.get("cancel_requested"))
            paused = current is not None and bool(current.get("pause_requested"))
            if paused and not canceled:
                current.update({"status": "paused", "updated_at": time.time()})
                QUEUE_CHANGED.notify_all()
            should_stop = canceled or paused
        if should_stop:
            raise DownloadStopped("Download stopped by user.")
        if event.get("status") == "downloading":
            downloaded = event.get("downloaded_bytes", 0)
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            percent = round(downloaded * 100 / total, 1) if isinstance(total, (int, float)) and total > 0 else None
            update_progress(job_id, {"status": "downloading", "progress": percent})
        elif event.get("status") == "finished":
            update_progress(job_id, {"status": "processing", "progress": 100})

    ffmpeg = shutil.which("ffmpeg")
    logger = DownloadLogger()
    max_height = QUALITY_HEIGHTS[quality]
    format_selector = (
        "bestvideo*+bestaudio/best"
        if max_height is None
        else f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]"
    )
    ydl_options: dict[str, object] = {
        "format": format_selector,
        "noplaylist": True,
        "ignoreerrors": False,
        "no_warnings": True,
        "quiet": True,
        "no_color": True,
        "cachedir": False,
        "outtmpl": str(job_directory / "%(title).120B [%(id)s].%(ext)s"),
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "ignoreconfig": True,
        "proxy": "",
        "progress_hooks": [progress_hook],
        "logger": logger,
        "ffmpeg_location": ffmpeg,
    }
    if COOKIES_FILE.is_file():
        ydl_options["cookiefile"] = str(COOKIES_FILE)
    try:
        update_progress(job_id, {"status": "downloading", "progress": 0})
        with yt_dlp.YoutubeDL(ydl_options) as downloader:
            try:
                exit_code = downloader.download([video_url])
            except DownloadStopped:
                raise
        if exit_code:
            raise RuntimeError(logger.error_message or "yt-dlp could not download this video.")
        files = [path for path in job_directory.iterdir() if path.is_file() and not path.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("The source did not provide a downloadable media file.")
        media_file = max(files, key=lambda path: path.stat().st_size)
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job.update({
                    "status": "complete",
                    "progress": 100,
                    "filename": media_file.name,
                    "size": media_file.stat().st_size,
                    "updated_at": time.time(),
                })
    except DownloadStopped:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                if job.get("cancel_requested"):
                    job.update({"status": "canceled", "progress": None, "updated_at": time.time()})
                else:
                    job.update({"status": "paused", "updated_at": time.time()})
                job.pop("pause_requested", None)
                job.pop("cancel_requested", None)
    except Exception as error:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is not None:
                job.update({
                    "status": "error",
                    "error": clean_error(error, video_url),
                    "updated_at": time.time(),
                })


def run_queue() -> None:
    global QUEUE_WORKER_RUNNING, ACTIVE_JOB_ID
    while True:
        with JOBS_LOCK:
            while ACTIVE_JOB_ID is not None:
                active = JOBS.get(ACTIVE_JOB_ID)
                if active is None or active.get("status") != "paused":
                    break
                QUEUE_CHANGED.wait(timeout=0.5)
            if not QUEUE:
                if ACTIVE_JOB_ID is None:
                    QUEUE_WORKER_RUNNING = False
                    return
                continue
            job_id = QUEUE.pop(0)
            job = JOBS.get(job_id)
            if job is None:
                continue
            job.update({"status": "downloading", "progress": 0, "updated_at": time.time()})
            ACTIVE_JOB_ID = job_id
            video_url = job["url"]
            quality = job["quality"]
        run_download(job_id, str(video_url), str(quality))
        with JOBS_LOCK:
            if ACTIVE_JOB_ID == job_id:
                ACTIVE_JOB_ID = None
                QUEUE_CHANGED.notify_all()


def purge_expired_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    expired: list[str] = []
    with JOBS_LOCK:
        for job_id, job in JOBS.items():
            updated_at = job.get("updated_at", cutoff)
            if job.get("status") in {"complete", "error", "canceled"} and isinstance(updated_at, (int, float)) and updated_at < cutoff:
                expired.append(job_id)
        for job_id in expired:
            JOBS.pop(job_id, None)
            if job_id in QUEUE:
                QUEUE.remove(job_id)
    for job_id in expired:
        shutil.rmtree(DOWNLOADS / job_id, ignore_errors=True)


class VideoDownloaderHandler(BaseHTTPRequestHandler):
    server_version = "Clipnest/1.0"

    def send_json(self, status: int, data: dict[str, object]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        purge_expired_jobs()
        request_path = urlsplit(self.path).path
        if request_path == "/api/health":
            self.send_json(200, {"status": "ok", "yt_dlp": yt_dlp.version.__version__, "ffmpeg": shutil.which("ffmpeg") is not None})
            return
        if request_path == "/api/jobs":
            with JOBS_LOCK:
                snapshots = [dict(job) for job in JOBS.values()]
            self.send_json(200, {"jobs": snapshots})
            return
        if request_path.startswith("/api/jobs/"):
            job_id = request_path.removeprefix("/api/jobs/")
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                snapshot = dict(job) if job is not None else None
            if snapshot is None:
                self.send_json(404, {"error": "Download job not found or expired."})
            else:
                self.send_json(200, snapshot)
            return
        if request_path.startswith("/api/files/"):
            self.send_download(request_path.removeprefix("/api/files/"))
            return

        relative_path = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
        file_path = (STATIC / relative_path).resolve()
        if not file_path.is_relative_to(STATIC.resolve()) or not file_path.is_file():
            self.send_error(404, "Not found")
            return
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        purge_expired_jobs()
        if urlsplit(self.path).path != "/api/download":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid request size."})
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self.send_json(413, {"error": "Request is empty or too large."})
            return
        try:
            body = json.loads(self.rfile.read(content_length))
            video_url = validate_video_url(body.get("url") if isinstance(body, dict) else None)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self.send_json(400, {"error": str(error)})
            return

        with JOBS_LOCK:
            try:
                quality = validate_quality(body.get("quality", "best"))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            pending = sum(job.get("status") in {"queued", "downloading", "processing"} for job in JOBS.values())
            if pending >= MAX_QUEUED_DOWNLOADS:
                self.send_json(429, {"error": f"The download queue is full ({MAX_QUEUED_DOWNLOADS} jobs maximum)."})
                return
            job_id = uuid.uuid4().hex
            queue_position = len(QUEUE) + 1
            JOBS[job_id] = {
                "id": job_id,
                "url": video_url,
                "quality": quality,
                "status": "queued",
                "progress": None,
                "position": queue_position,
                "updated_at": time.time(),
            }
            QUEUE.append(job_id)
            for position, queued_id in enumerate(QUEUE, start=1):
                queued_job = JOBS.get(queued_id)
                if queued_job is not None and queued_job.get("status") == "queued":
                    queued_job["position"] = position
            global QUEUE_WORKER_RUNNING
            if not QUEUE_WORKER_RUNNING:
                QUEUE_WORKER_RUNNING = True
                threading.Thread(target=run_queue, daemon=True).start()
        self.send_json(202, {"id": job_id, "status": "queued"})

    def do_PATCH(self) -> None:
        purge_expired_jobs()
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "jobs"] or parts[3] not in {"stop", "resume", "cancel"}:
            self.send_json(404, {"error": "Not found"})
            return
        job_id, action = parts[2], parts[3]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                self.send_json(404, {"error": "Download job not found or expired."})
                return
            status = job.get("status")
            if action == "stop":
                if status == "queued":
                    if job_id in QUEUE:
                        QUEUE.remove(job_id)
                    job.update({"status": "paused", "position": None, "updated_at": time.time()})
                elif status in {"downloading", "processing"}:
                    job["pause_requested"] = True
                else:
                    self.send_json(409, {"error": "Only queued or active jobs can be stopped."})
                    return
            elif action == "resume":
                if status != "paused":
                    self.send_json(409, {"error": "Only paused jobs can be resumed."})
                    return
                if ACTIVE_JOB_ID == job_id:
                    job.update({"status": "downloading", "updated_at": time.time()})
                    QUEUE_CHANGED.notify_all()
                else:
                    job.update({"status": "queued", "progress": None, "updated_at": time.time()})
                    QUEUE.append(job_id)
                    job["position"] = len(QUEUE)
                    global QUEUE_WORKER_RUNNING
                    if not QUEUE_WORKER_RUNNING:
                        QUEUE_WORKER_RUNNING = True
                        threading.Thread(target=run_queue, daemon=True).start()
            else:
                if status in {"complete", "error", "canceled"}:
                    self.send_json(409, {"error": "This job can no longer be canceled."})
                    return
                if status in {"downloading", "processing"}:
                    job["cancel_requested"] = True
                else:
                    if job_id in QUEUE:
                        QUEUE.remove(job_id)
                    job.update({"status": "canceled", "position": None, "updated_at": time.time()})
                QUEUE_CHANGED.notify_all()
            snapshot = dict(job)
        self.send_json(200, snapshot)

    def do_DELETE(self) -> None:
        purge_expired_jobs()
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "jobs"]:
            self.send_json(404, {"error": "Not found"})
            return
        job_id = parts[2]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                self.send_json(404, {"error": "Download job not found or expired."})
                return
            if job.get("status") in {"queued", "downloading", "processing", "paused"}:
                self.send_json(409, {"error": "Cancel or stop this job before deleting it."})
                return
            JOBS.pop(job_id, None)
        shutil.rmtree(DOWNLOADS / job_id, ignore_errors=True)
        self.send_json(200, {"id": job_id, "status": "deleted"})

    def send_download(self, job_id: str) -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            file_name = job.get("filename") if job and job.get("status") == "complete" else None
        if not isinstance(file_name, str):
            self.send_json(404, {"error": "This download is not ready or has expired."})
            return
        job_directory = (DOWNLOADS / job_id).resolve()
        file_path = (job_directory / file_name).resolve()
        if not file_path.is_relative_to(job_directory) or not file_path.is_file():
            self.send_json(404, {"error": "Downloaded file is no longer available."})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(file_path.stat().st_size))
        safe_filename = "".join(character if character.isascii() and (character.isalnum() or character in "._ -") else "_" for character in file_path.name)
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with file_path.open("rb") as media:
            shutil.copyfileobj(media, self.wfile)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    address = ("127.0.0.1", 8000)
    DOWNLOADS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer(address, VideoDownloaderHandler)
    ffmpeg_status = shutil.which("ffmpeg") or "not found (separate audio/video streams may not merge)"
    print(f"Local downloader UI running at http://{address[0]}:{address[1]}")
    print(f"yt-dlp {yt_dlp.version.__version__}; FFmpeg: {ffmpeg_status}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
