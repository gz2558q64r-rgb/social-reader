import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

WHISPER_MODEL = "large-v3"

job = json.loads((ROOT / "job.json").read_text(encoding="utf-8"))
url = job["url"].strip()

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

def run(cmd):
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-12000:],
        "stderr": proc.stderr[-12000:],
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
    "errors": [],
}

ydl = run([
    "yt-dlp",
    "--no-playlist",
    "--write-info-json",
    "--write-subs",
    "--write-auto-subs",
    "--sub-langs", "ja.*,en.*",
    "--sub-format", "vtt",
    "--restrict-filenames",
    "-o", str(OUT / "video.%(ext)s"),
    url,
])
result["method"].append("yt-dlp")
if ydl["returncode"] != 0:
    result["errors"].append({"stage": "yt-dlp", "detail": ydl["stderr"]})

info_files = list(OUT.glob("video.info.json"))
if info_files:
    try:
        info = json.loads(info_files[0].read_text(encoding="utf-8"))
        result["title"] = info.get("title") or info.get("description")
        result["author"] = info.get("uploader") or info.get("channel")
        result["duration"] = info.get("duration")
    except Exception as exc:
        result["errors"].append({"stage": "info-json", "detail": str(exc)})

subtitle_files = sorted(list(OUT.glob("video*.vtt")) + list(OUT.glob("video*.srt")))
transcript = ""
if subtitle_files:
    try:
        raw = subtitle_files[0].read_text(encoding="utf-8", errors="ignore")
        transcript = clean_vtt(raw) if subtitle_files[0].suffix == ".vtt" else raw
        if transcript.strip():
            (OUT / "transcript.txt").write_text(transcript, encoding="utf-8")
            result["transcript_source"] = "platform_subtitles"
            result["method"].append("subtitles")
    except Exception as exc:
        result["errors"].append({"stage": "subtitles", "detail": str(exc)})

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

    if not transcript.strip():
        audio = OUT / "audio.wav"
        audio_run = run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000",
            str(audio),
        ])
        if audio_run["returncode"] == 0 and audio.exists():
            try:
                model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
                segments, info = model.transcribe(str(audio), vad_filter=True)
                transcript = "\n".join(
                    segment.text.strip() for segment in segments if segment.text.strip()
                )
                if transcript:
                    (OUT / "transcript.txt").write_text(transcript, encoding="utf-8")
                    result["transcript_source"] = "faster-whisper"
                    result["whisper_model"] = WHISPER_MODEL
                    result["method"].append("faster-whisper")
            except Exception as exc:
                result["errors"].append({"stage": "whisper", "detail": str(exc)})
        else:
            result["errors"].append({"stage": "audio", "detail": audio_run["stderr"]})

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
(OUT / "yt-dlp.log.txt").write_text(
    "STDOUT\n" + ydl["stdout"] + "\n\nSTDERR\n" + ydl["stderr"],
    encoding="utf-8",
)

print(json.dumps(result, ensure_ascii=False, indent=2))
