import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from youtube_transcript_api import YouTubeTranscriptApi

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

WHISPER_MODEL = "large-v3"
# Keep enough time for the caption fallback before the GitHub Actions job limit.
WHISPER_TIMEOUT_SECONDS = int(os.environ.get("WHISPER_TIMEOUT_SECONDS", "1200"))

def youtube_video_id(value: str):
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        return parsed.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        if parsed.path == "/watch":
            from urllib.parse import parse_qs
            return parse_qs(parsed.query).get("v", [None])[0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1]
    return None

def platform_from_url(value: str) -> str:
    host = urlparse(value).netloc.lower()
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    if "x.com" in host or "twitter.com" in host:
        return "x"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "unknown"

def run(cmd, timeout=None):
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-12000:],
            "stderr": proc.stderr[-12000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": 124,
            "stdout": "",
            "stderr": f"Timed out after {timeout} seconds: {exc}",
        }

def clean_vtt(text: str) -> str:
    lines = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if "-->" in line:
            continue
        if re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"&nbsp;", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)
    return "\n".join(lines)

def subtitle_sort_key(path: Path):
    name = path.name.lower()
    if ".ja" in name:
        return (0, name)
    if ".en" in name:
        return (1, name)
    return (2, name)

def main():
    job = json.loads((ROOT / "job.json").read_text(encoding="utf-8"))
    url = job["url"].strip()
    whisper_model = job.get("whisper_model", WHISPER_MODEL)

    result = {
        "url": url,
        "platform": platform_from_url(url),
        "status": "started",
        "method": [],
        "title": None,
        "author": None,
        "duration": None,
        "transcript_source": None,
        "whisper_model": None,
        "transcription_attempts": [],
        "errors": [],
    }

    transcript = ""

    # X text/image fallback. yt-dlp's Twitter extractor focuses on playable media,
    # so text-only/image-only posts can otherwise look like "no content".
    if result["platform"] == "x":
        try:
            match = re.search(r"/status/(\d+)", url)
            if match:
                tweet_id = match.group(1)
                req = Request(
                    f"https://api.fxtwitter.com/status/{tweet_id}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urlopen(req, timeout=30) as resp:
                    fx_data = json.loads(resp.read().decode("utf-8", errors="replace"))
                tweet = fx_data.get("tweet") or {}
                text_value = (tweet.get("text") or "").strip()
                if text_value:
                    result["text"] = text_value
                    result["title"] = text_value
                author = tweet.get("author") or {}
                result["author"] = (
                    author.get("name")
                    or author.get("screen_name")
                    or result.get("author")
                )
                media = tweet.get("media") or {}
                photos = media.get("photos") or []
                for idx, photo in enumerate(photos[:8], start=1):
                    photo_url = photo.get("url") if isinstance(photo, dict) else None
                    if not photo_url:
                        continue
                    image_req = Request(photo_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urlopen(image_req, timeout=30) as image_resp:
                        (OUT / f"frame_{idx:02d}.jpg").write_bytes(image_resp.read())
                result["method"].append("fxtwitter-api")
        except Exception as exc:
            result["errors"].append({"stage": "fxtwitter-api", "detail": str(exc)})

    # Download media first, without asking for any subtitles. Captions are a
    # fallback only if local Whisper transcription cannot produce text.
    ydl_common = [
        "yt-dlp",
        "--no-playlist",
        "--write-info-json",
        "--restrict-filenames",
        "-o", str(OUT / "video.%(ext)s"),
    ]
    ydl_runs = []
    ydl = run(ydl_common + [url])
    ydl_runs.append(("yt-dlp-media", ydl))
    result["method"].append("yt-dlp-media")
    if ydl["returncode"] != 0:
        result["errors"].append({"stage": "yt-dlp-media", "detail": ydl["stderr"]})

    info_files = list(OUT.glob("video.info.json"))
    if info_files:
        try:
            info = json.loads(info_files[0].read_text(encoding="utf-8"))
            result["title"] = info.get("title") or info.get("description")
            result["author"] = info.get("uploader") or info.get("channel")
            result["duration"] = info.get("duration")
        except Exception as exc:
            result["errors"].append({"stage": "info-json", "detail": str(exc)})

    video_candidates = [
        p for p in OUT.glob("video.*")
        if p.suffix.lower() not in {".json", ".vtt", ".srt", ".part", ".ytdl"}
    ]
    video = video_candidates[0] if video_candidates else None

    if video:
        result["method"].append("ffmpeg")

        frames = run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video),
            "-vf", "fps=1/5,scale='min(960,iw)':-2",
            "-frames:v", "8",
            str(OUT / "frame_%02d.jpg"),
        ])
        if frames["returncode"] != 0:
            result["errors"].append({"stage": "frames", "detail": frames["stderr"]})

        # Whisper large-v3 is the primary recognizer; subtitle availability
        # must never suppress this attempt.
        audio = OUT / "audio.wav"
        audio_run = run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
            str(audio),
        ])
        result["transcription_attempts"].append("faster-whisper")
        if audio_run["returncode"] == 0 and audio.exists():
            whisper_out = OUT / "whisper-transcript.txt"
            whisper = run([
                sys.executable, str(ROOT / "whisper_worker.py"),
                str(audio), str(whisper_out), whisper_model,
            ], timeout=WHISPER_TIMEOUT_SECONDS)
            if whisper["returncode"] == 0 and whisper_out.exists():
                transcript = whisper_out.read_text(encoding="utf-8").strip()
            if transcript:
                result["transcript_source"] = "faster-whisper"
                result["whisper_model"] = whisper_model
                result["method"].append("faster-whisper")
            else:
                result["errors"].append({
                    "stage": "whisper",
                    "detail": whisper["stderr"] or "Whisper returned an empty transcript",
                })
        else:
            result["errors"].append({"stage": "audio", "detail": audio_run["stderr"]})

    # Only attempt captions after Whisper has failed, timed out, or there is
    # no usable video/audio. Never overwrite a successful Whisper transcript.
    if not transcript and result["platform"] == "youtube":
        video_id = youtube_video_id(url)
        if video_id:
            result["transcription_attempts"].append("youtube-transcript-api")
            try:
                fetched = YouTubeTranscriptApi().fetch(video_id, languages=["ja", "en"])
                transcript = "\n".join(
                    str(getattr(item, "text", None) or
                        (item.get("text") if isinstance(item, dict) else "")).strip()
                    for item in fetched
                ).strip()
                if transcript:
                    result["transcript_source"] = "youtube-transcript-api"
                    result["method"].append("youtube-transcript-api")
            except Exception as exc:
                result["errors"].append({"stage": "youtube-transcript-api", "detail": str(exc)})

    if not transcript:
        # Use yt-dlp subtitle tracks if YouTubeTranscriptApi was unavailable.
        # Japanese and English requests are independent to prevent rate-limit
        # failures on one language from breaking the other.
        languages = ["ja.*", "en.*"] if result["platform"] == "youtube" else ["ja.*,en.*"]
        for language in languages:
            label = f"yt-dlp-subs-{language}"
            result["transcription_attempts"].append(label)
            subtitles = run([
                "yt-dlp", "--no-playlist", "--skip-download",
                "--write-subs", "--write-auto-subs",
                "--sub-langs", language, "--sub-format", "vtt",
                "--restrict-filenames",
                "-o", str(OUT / "video.%(ext)s"), url,
            ])
            ydl_runs.append((label, subtitles))
            if subtitles["returncode"] != 0:
                result["errors"].append({"stage": label, "detail": subtitles["stderr"]})
            subtitle_files = sorted(
                list(OUT.glob("video*.vtt")) + list(OUT.glob("video*.srt")),
                key=subtitle_sort_key,
            )
            for path in subtitle_files:
                raw = path.read_text(encoding="utf-8", errors="replace")
                candidate = clean_vtt(raw) if path.suffix == ".vtt" else raw.strip()
                if candidate:
                    transcript = candidate
                    result["transcript_source"] = "platform_subtitles"
                    result["method"].append("subtitles")
                    break
            if transcript:
                break

    if not transcript and result["platform"] == "youtube":
        video_id = youtube_video_id(url)
        if video_id:
            result["transcription_attempts"].append("hosted-transcript")
            try:
                req = Request(
                    f"https://youtube-transcript.ai/transcript/{video_id}.txt?lang=ja",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                with urlopen(req, timeout=30) as resp:
                    transcript = resp.read().decode("utf-8", errors="replace").strip()
                if transcript:
                    result["transcript_source"] = "youtube-transcript.ai"
                    result["method"].append("hosted-transcript")
            except Exception as exc:
                result["errors"].append({"stage": "hosted-transcript", "detail": str(exc)})

    if transcript:
        (OUT / "transcript.txt").write_text(transcript, encoding="utf-8")

    result["video_found"] = bool(video)
    result["frame_count"] = len(list(OUT.glob("frame_*.jpg")))
    result["transcript_found"] = bool((OUT / "transcript.txt").exists())
    result["status"] = "success" if (
        result["transcript_found"] or result["frame_count"] > 0 or result["title"]
    ) else "no_content"

    (OUT / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log_parts = []
    for label, ydl_result in ydl_runs:
        log_parts.append(
            f"## {label}\n"
            + "STDOUT\n" + ydl_result["stdout"]
            + "\n\nSTDERR\n" + ydl_result["stderr"]
        )
    (OUT / "yt-dlp.log.txt").write_text(
        "\n\n".join(log_parts),
        encoding="utf-8",
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
