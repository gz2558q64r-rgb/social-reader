# social-reader

Public social-media URL -> machine-readable metadata, transcript, and representative frames.

The project is intentionally simple: use platform metadata/subtitles first, then `yt-dlp`, `ffmpeg`, and local `faster-whisper` when needed. No external LLM API is required.

## Current v0

- Input: `job.json`
- Supported first path: public Instagram/TikTok/YouTube URLs that `yt-dlp` can access
- Output artifact:
  - `output/result.json`
  - `output/transcript.txt` when subtitles or speech are available
  - up to 8 representative JPG frames
  - diagnostic `yt-dlp.log.txt`
- Intermediate `video.*` and `audio.wav` are used only during the run and are **not retained in the GitHub Actions artifact**
- Runtime: GitHub Actions on `ubuntu-latest`

## Run locally

```bash
pip install -r requirements.txt
sudo apt-get install ffmpeg
python reader.py
```

Edit `job.json` and set the URL you want to inspect. Pushing a change to `job.json` triggers the GitHub Actions workflow.

## Design

The extraction order is deliberately deterministic:

1. platform subtitles / metadata
2. `yt-dlp`
3. `ffmpeg` frame and audio extraction
4. local `faster-whisper`

Browser automation is a later fallback, not the default path.
