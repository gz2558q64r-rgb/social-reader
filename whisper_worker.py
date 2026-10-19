"""Run Whisper in an isolated process so the caller can time it out safely."""
import sys
from pathlib import Path

from faster_whisper import WhisperModel


def main():
    if len(sys.argv) != 4:
        raise SystemExit("Usage: whisper_worker.py AUDIO OUTPUT MODEL")

    audio = Path(sys.argv[1])
    output = Path(sys.argv[2])
    model_name = sys.argv[3]

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio), vad_filter=True)
    transcript = "\n".join(
        segment.text.strip() for segment in segments if segment.text.strip()
    )
    if not transcript.strip():
        raise SystemExit("Whisper produced no speech transcript")
    output.write_text(transcript, encoding="utf-8")


if __name__ == "__main__":
    main()
