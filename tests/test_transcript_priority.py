import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import reader


class TranscriptPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "output"
        self.out.mkdir()
        (self.root / "job.json").write_text(
            json.dumps({"url": "https://youtu.be/a469QUpSUko"}), encoding="utf-8"
        )
        self.calls = []
        self.whisper_result = "Whisper が聞き取った音声"
        self.api_result = "YouTube の自動字幕"
        self.media_available = True
        self.caption_file_available = False
        for key, value in (("ROOT", self.root), ("OUT", self.out)):
            guard = patch.object(reader, key, value)
            guard.start()
            self.addCleanup(guard.stop)

    def fake_run(self, cmd, timeout=None):
        self.calls.append((cmd, timeout))
        if cmd[0] == "yt-dlp":
            if "--skip-download" in cmd:
                if self.caption_file_available:
                    (self.out / "video.ja.vtt").write_text(
                        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n日本語字幕",
                        encoding="utf-8",
                    )
                return {"returncode": 0, "stdout": "", "stderr": ""}
            if not self.media_available:
                return {"returncode": 1, "stdout": "", "stderr": "download failed"}
            (self.out / "video.mp4").write_bytes(b"mock video")
            (self.out / "video.info.json").write_text(
                '{"title": "test video", "duration": 30}', encoding="utf-8"
            )
        elif cmd[0] == "ffmpeg":
            if "-vn" in cmd:
                (self.out / "audio.wav").write_bytes(b"mock audio")
            else:
                (self.out / "frame_01.jpg").write_bytes(b"mock image")
        elif cmd[0] == sys.executable:
            if self.whisper_result is None:
                return {"returncode": 124, "stdout": "", "stderr": "Timed out"}
            (self.out / "whisper-transcript.txt").write_text(
                self.whisper_result, encoding="utf-8"
            )
        return {"returncode": 0, "stdout": "", "stderr": ""}

    def execute(self, api_unavailable=False):
        with patch.object(reader, "run", side_effect=self.fake_run):
            with patch.object(reader, "YouTubeTranscriptApi") as api:
                if api_unavailable:
                    api.return_value.fetch.side_effect = RuntimeError("unavailable")
                else:
                    api.return_value.fetch.return_value = [
                        SimpleNamespace(text=self.api_result)
                    ]
                with contextlib.redirect_stdout(io.StringIO()):
                    reader.main()
                return (
                    json.loads((self.out / "result.json").read_text(encoding="utf-8")),
                    api,
                )

    def test_whisper_wins_even_when_captions_exist(self):
        result, api = self.execute()
        self.assertEqual(result["transcript_source"], "faster-whisper")
        self.assertEqual(result["whisper_model"], "large-v3")
        self.assertEqual(result["transcription_attempts"], ["faster-whisper"])
        self.assertEqual(
            (self.out / "transcript.txt").read_text(encoding="utf-8"),
            self.whisper_result,
        )
        api.assert_not_called()
        self.assertFalse(any("--skip-download" in cmd for cmd, _ in self.calls))
        worker = [(cmd, timeout) for cmd, timeout in self.calls
                  if cmd[0] == sys.executable]
        self.assertEqual(len(worker), 1)
        self.assertEqual(worker[0][1], reader.WHISPER_TIMEOUT_SECONDS)

    def test_whisper_timeout_falls_back_to_youtube_api(self):
        self.whisper_result = None
        result, api = self.execute()
        self.assertEqual(result["transcript_source"], "youtube-transcript-api")
        self.assertEqual(result["transcription_attempts"][:2], [
            "faster-whisper", "youtube-transcript-api",
        ])
        self.assertTrue(any(e["stage"] == "whisper" for e in result["errors"]))
        self.assertEqual(
            (self.out / "transcript.txt").read_text(encoding="utf-8"),
            self.api_result,
        )
        api.return_value.fetch.assert_called_once()

    def test_whisper_and_api_fail_use_yt_dlp_subtitles(self):
        self.whisper_result = None
        self.caption_file_available = True
        result, _ = self.execute(api_unavailable=True)
        self.assertEqual(result["transcript_source"], "platform_subtitles")
        self.assertIn("yt-dlp-subs-ja.*", result["transcription_attempts"])
        self.assertEqual(
            (self.out / "transcript.txt").read_text(encoding="utf-8"),
            "日本語字幕",
        )

    def test_unavailable_media_can_use_youtube_captions(self):
        self.media_available = False
        result, _ = self.execute()
        self.assertFalse(result["video_found"])
        self.assertEqual(result["transcript_source"], "youtube-transcript-api")
        self.assertNotIn("faster-whisper", result["transcription_attempts"])

    def test_subprocess_timeout_returns_failure_for_fallback(self):
        with patch.object(
            reader.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd=["worker"], timeout=5),
        ):
            res = reader.run(["worker"], timeout=5)
        self.assertEqual(res["returncode"], 124)
        self.assertIn("Timed out", res["stderr"])


if __name__ == "__main__":
    unittest.main()
