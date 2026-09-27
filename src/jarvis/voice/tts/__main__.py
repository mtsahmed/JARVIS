"""Manual TTS check:
    python -m jarvis.voice.tts "Hello Ahmed"                        # local voice
    python -m jarvis.voice.tts "Hello Ahmed" --provider elevenlabs  # premium (uses quota)
    python -m jarvis.voice.tts --quota                              # ElevenLabs characters left
"""

from __future__ import annotations

import argparse
import sys

from jarvis.core.config import PROJECT_ROOT, ConfigError, load_voice_config
from jarvis.core.logging_setup import setup_logging
from jarvis.voice.tts.base import TTSError
from jarvis.voice.tts.factory import create_speaker


def main() -> int:
    parser = argparse.ArgumentParser(description="Speak a line of text with Jarvis's voice.")
    parser.add_argument("text", nargs="?", default="Hello, I am Jarvis.")
    parser.add_argument("--provider", choices=("sapi", "elevenlabs"), default="sapi")
    parser.add_argument("--quota", action="store_true", help="show ElevenLabs characters left and exit")
    args = parser.parse_args()

    try:
        config = load_voice_config()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1
    setup_logging(PROJECT_ROOT / "logs", secrets=config.secrets)

    try:
        if args.quota:
            from jarvis.voice.tts.elevenlabs_adapter import ElevenLabsAdapter

            adapter = ElevenLabsAdapter(config.elevenlabs_api_key or "", config.elevenlabs_voice_id or "",
                                        config.elevenlabs_model or "")
            print(f"ElevenLabs characters left this period: {adapter.characters_left():,}")
            return 0
        speaker = create_speaker(config, purpose="test", force_premium=args.provider == "elevenlabs")
        used = speaker.speak(args.text)
    except TTSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"Spoke {len(args.text)} characters with: {used}" + (" (fallback)" if used != args.provider else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
