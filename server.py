
import os
import re
import json
import shutil
import tempfile
import subprocess
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl

app = FastAPI(title="SnapMega Download API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

ALLOWED_HOSTS = (
    "tiktok.com", "instagram.com", "facebook.com", "fb.watch",
    "youtube.com", "youtu.be", "youtube-nocookie.com",
)

semaphore = threading.BoundedSemaphore(3)


class DownloadRequest(BaseModel):
    url: HttpUrl
    mode: str = "video"
    clip_start: str = "00:10"
    clip_end: str = "01:00"


def validate_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower().rstrip(".")

    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="رابط غير صالح")

    if not any(
        host == domain or host.endswith("." + domain)
        for domain in ALLOWED_HOSTS
    ):
        raise HTTPException(status_code=400, detail="هذه المنصة غير مدعومة")

    return raw_url


def safe_filename(name: str, mode: str) -> str:
    name = Path(name).name
    name = re.sub(
        r"[^\w .()\-\[\]]+", "_", name, flags=re.UNICODE
    ).strip(" .")

    if not name:
        name = "snapmega_download"

    suffix = ".mp3" if mode == "audio" else ".mp4"
    stem = Path(name).stem if Path(name).suffix else name
    return stem[:80] + suffix


def parse_time_to_seconds(value: str) -> int:
    parts = str(value).strip().split(":")

    if not 1 <= len(parts) <= 3:
        raise ValueError("صيغة الوقت غير صحيحة")

    if any(not part.isdigit() for part in parts):
        raise ValueError("صيغة الوقت غير صحيحة")

    nums = [int(part) for part in parts]

    if len(nums) == 1:
        return nums[0]

    if len(nums) == 2:
        if nums[1] >= 60:
            raise ValueError("الثواني غير صحيحة")
        return nums[0] * 60 + nums[1]

    if nums[1] >= 60 or nums[2] >= 60:
        raise ValueError("صيغة الوقت غير صحيحة")

    return nums[0] * 3600 + nums[1] * 60 + nums[2]


# TikTok: keep the original TikWM URL selection and fallback behavior.
def get_tiktok_direct(raw_url: str, mode: str, temp_dir: str):
    api_url = "https://www.tikwm.com/api/"
    data = urllib.parse.urlencode({
        "url": raw_url,
        "hd": 1,
    }).encode("utf-8")

    req = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            )
        },
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        res = json.loads(response.read().decode("utf-8"))

    if res.get("code") != 0 or "data" not in res:
        raise RuntimeError("TikWM API returned an error")

    vdata = res["data"]

    if mode == "audio":
        target_url = vdata.get("music")
        ext = ".mp3"
    else:
        target_url = (
            vdata.get("play")
            or vdata.get("hdplay")
            or vdata.get("wmplay")
        )
        ext = ".mp4"

    if not target_url:
        raise RuntimeError("No TikTok media URL found")

    out_file = os.path.join(
        temp_dir,
        f"tiktok_{vdata.get('id', 'video')}{ext}",
    )

    file_req = urllib.request.Request(
        target_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
    )

    with urllib.request.urlopen(file_req, timeout=60) as response:
        with open(out_file, "wb") as output:
            shutil.copyfileobj(response, output)

    return out_file


def probe_media(path: str) -> dict:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name:format=duration",
            "-of", "json",
            path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        raise RuntimeError("Could not inspect media file")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    stream_types = {
        stream.get("codec_type") for stream in streams
    }

    try:
        duration = float(
            data.get("format", {}).get("duration", 0) or 0
        )
    except (TypeError, ValueError):
        duration = 0

    return {
        "has_video": "video" in stream_types,
        "has_audio": "audio" in stream_types,
        "duration": duration,
    }


def validate_video(path: str) -> dict:
    if not os.path.isfile(path):
        raise RuntimeError("Media file does not exist")

    if os.path.getsize(path) < 5 * 1024:
        raise RuntimeError("Media file is too small")

    info = probe_media(path)

    if not info["has_video"]:
        raise RuntimeError("File contains no video stream")

    if info["duration"] <= 0.2:
        raise RuntimeError("Invalid video duration")

    return info


def make_compatible_mp4(source: str, temp_dir: str) -> str:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg/ffprobe is not installed")

    info = validate_video(source)
    output = os.path.join(temp_dir, "snapmega_final.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-i", source,
        "-map", "0:v:0",
    ]

    if info["has_audio"]:
        cmd += ["-map", "0:a:0"]
    else:
        cmd += ["-an"]

    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
    ]

    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd += [
        "-movflags", "+faststart",
        "-sn",
        "-dn",
        output,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=240,
    )

    if result.returncode != 0 or not os.path.isfile(output):
        print("FFmpeg conversion failed:", (result.stderr or "")[-4000:])
        raise RuntimeError("MP4 conversion failed")

    validate_video(output)
    return output


def download_with_ytdlp(
    raw_url: str,
    mode: str,
    request: DownloadRequest,
    temp_dir: str,
) -> str:
    output_template = os.path.join(
        temp_dir, "media_%(id)s.%(ext)s"
    )

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--force-overwrites",
        "--socket-timeout", "30",
        "--retries", "2",
        "--user-agent",
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "-o", output_template,
    ]

    host = (urlparse(raw_url).hostname or "").lower()

    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
        cmd += [
            "--extractor-args",
            "youtube:player_client=android,web",
        ]

    elif host == "instagram.com" or host.endswith(".instagram.com"):
        cmd += [
            "--add-header",
            "Accept-Language:en-US,en;q=0.9",
        ]

    elif (
        host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fb.watch"
    ):
        cmd += [
            "--add-header",
            "Accept-Language:en-US,en;q=0.9",
        ]

    if mode == "audio":
        cmd += [
            "-f", "bestaudio/best",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--postprocessor-args", "ffmpeg:-ar 44100",
        ]

    else:
        # Select video + audio where possible, or a combined format.
        cmd += [
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
        ]

        if mode == "clip":
            try:
                start = parse_time_to_seconds(request.clip_start)
                end = parse_time_to_seconds(request.clip_end)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="صيغة وقت القص غير صحيحة",
                )

            if end <= start:
                raise HTTPException(
                    status_code=400,
                    detail="وقت النهاية يجب أن يكون بعد وقت البداية",
                )

            cmd += [
                "--download-sections", f"*{start}-{end}",
                "--force-keyframes-at-cuts",
            ]

    cmd.append(raw_url)

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        print("yt-dlp timeout for host:", host)
        raise HTTPException(
            status_code=504,
            detail="انتهت مهلة التنزيل، حاول مجدداً",
        )

    if result.returncode != 0:
        print("yt-dlp failed for host:", host)
        print("yt-dlp exit code:", result.returncode)
        print("yt-dlp stderr:", (result.stderr or "")[-5000:])
        raise RuntimeError("yt-dlp download failed")

    allowed_extensions = {
        ".mp4", ".mkv", ".webm", ".mov", ".m4v",
        ".mp3", ".m4a", ".opus", ".wav",
    }

    candidates = []

    for filename in os.listdir(temp_dir):
        path = os.path.join(temp_dir, filename)

        if not os.path.isfile(path):
            continue

        if filename.endswith((".part", ".ytdl", ".temp")):
            continue

        if Path(filename).suffix.lower() not in allowed_extensions:
            continue

        if os.path.getsize(path) < 5 * 1024:
            continue

        candidates.append(path)

    if not candidates:
        print("yt-dlp produced no usable media")
        print("yt-dlp stderr:", (result.stderr or "")[-5000:])
        raise RuntimeError("No usable output file")

    # Prefer the output generated by this request.
    candidates.sort(
        key=lambda path: (
            os.path.basename(path).startswith("media_"),
            os.path.getsize(path),
        ),
        reverse=True,
    )

    selected = candidates[0]

    if mode != "audio":
        validate_video(selected)

    return selected


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "snapmega-api",
        "yt_dlp": shutil.which("yt-dlp") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }


@app.post("/api/download")
def download_media(request: DownloadRequest):
    raw_url = validate_url(str(request.url))
    mode = request.mode.lower().strip()

    if mode not in ("video", "audio", "clip"):
        raise HTTPException(
            status_code=400,
            detail="نوع التحميل غير مدعوم",
        )

    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="السيرفر مشغول حالياً، يرجى المحاولة بعد لحظات",
        )

    temp_dir = tempfile.mkdtemp(prefix="snapmega_")

    try:
        if not shutil.which("yt-dlp"):
            raise HTTPException(
                status_code=500,
                detail="yt-dlp غير مثبت على السيرفر",
            )

        host = (urlparse(raw_url).hostname or "").lower()
        is_tiktok = host == "tiktok.com" or host.endswith(".tiktok.com")

        target_file = None
        used_tiktok_direct = False

        # Keep the original TikTok direct-download route unchanged.
        if is_tiktok:
            try:
                target_file = get_tiktok_direct(
                    raw_url, mode, temp_dir
                )
                used_tiktok_direct = True
            except Exception as exc:
                print(
                    "TikTok direct failed, fallback to yt-dlp:",
                    repr(exc),
                )

        # Facebook, Instagram and YouTube use yt-dlp.
        if not target_file or not os.path.isfile(target_file):
            target_file = download_with_ytdlp(
                raw_url, mode, request, temp_dir
            )
            used_tiktok_direct = False

        if not os.path.isfile(target_file):
            raise HTTPException(
                status_code=400,
                detail="لم يتم العثور على الملف الناتج",
            )

        if os.path.getsize(target_file) < 5 * 1024:
            raise HTTPException(
                status_code=400,
                detail="الملف المستخرج غير صالح أو ناقص",
            )

        if mode != "audio":
            # Preserve TikTok's successful direct file as before.
            # Validate and convert non-TikTok / yt-dlp output.
            if not used_tiktok_direct:
                try:
                    target_file = make_compatible_mp4(
                        target_file, temp_dir
                    )
                except Exception as exc:
                    print("Video validation/conversion failed:", repr(exc))
                    raise HTTPException(
                        status_code=400,
                        detail="تعذر تجهيز الفيديو. راجع Logs في Render",
                    )

        final_filename = safe_filename(
            os.path.basename(target_file), mode
        )

        return FileResponse(
            path=target_file,
            filename=final_filename,
            media_type=(
                "audio/mpeg" if mode == "audio"
                else "video/mp4"
            ),
        )

    except HTTPException:
        raise

    except Exception as exc:
        print("Download endpoint error:", repr(exc))
        raise HTTPException(
            status_code=400,
            detail="تعذر تنزيل الوسيط. راجع Logs في Render لمعرفة السبب",
        )

    finally:
        semaphore.release()
