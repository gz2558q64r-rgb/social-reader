# social-reader

Public social-media URL -> machine-readable metadata, transcript, and representative frames.

The project prioritizes local `faster-whisper` **large-v3** speech recognition. When local transcription fails, times out, or media/audio is unavailable, it falls back to platform subtitles. No external LLM API is required.

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

1. `yt-dlp` media + metadata download (without requesting subtitles)
2. `ffmpeg` frame and audio extraction
3. Local `faster-whisper` large-v3 transcription, with a 20-minute maximum per attempt
4. **Only if Whisper fails or returns no text**: YouTube transcript API, `yt-dlp` subtitles (Japanese then English), then hosted subtitle fallback

Browser automation is a later fallback, not the default path.

Whisper's subprocess timeout is configurable via `WHISPER_TIMEOUT_SECONDS` (default 1200). The GitHub Actions reader job has a 30-minute cap to leave time for subtitle fallback. `output/result.json` includes `transcription_attempts`, `transcript_source`, and recorded errors. The optional Whisper model download/inference may be expensive on a CPU runner.

Run fast mocked regression tests with `python -m unittest discover -s tests -v` (requires `youtube-transcript-api`).
