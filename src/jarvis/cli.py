"""`jarvis` command: text chat, or `jarvis --voice` for push-to-talk. print() is allowed here (CLI UI only)."""

from __future__ import annotations

import argparse
import logging
import sys

from jarvis.core.chat import ChatSession
from jarvis.core.config import Config, ConfigError, load_config
from jarvis.core.logging_setup import setup_logging
from jarvis.llm.base import LLMAdapter, LLMError
from jarvis.llm.factory import create_llm
from jarvis.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

HELP = "Commands: /reset clears history, /exit quits."


class TextConfirmer:
    """Asks in the terminal before a CONFIRM-tier action. Only y/yes approves; anything else is no."""

    def confirm(self, question: str) -> bool:
        try:
            answer = input(f"jarvis> {question} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")


def build_tools() -> ToolRegistry:
    """All tools Jarvis may use. Each declares its permission tier."""
    return ToolRegistry([])


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal assistant.")
    parser.add_argument("--voice", action="store_true", help="push-to-talk voice mode (falls back to text)")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    setup_logging(config.logs_dir, secrets=config.secrets)
    logger.info("Jarvis starting: %s", config)

    try:
        llm = create_llm(config)
    except LLMError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.voice and run_voice(config, llm):
        logger.info("Jarvis stopped")
        return 0
    run_text(config, ChatSession(llm, history_turns=config.history_turns, tools=build_tools()))
    logger.info("Jarvis stopped")
    return 0


def run_voice(config: Config, llm: LLMAdapter) -> bool:
    """Run voice mode until Ctrl+C. Returns False if voice could not start (caller falls back to text)."""
    # Imported here so text mode never loads the audio/speech libraries.
    from jarvis.core.config import load_voice_config
    from jarvis.voice.audio import AudioError, Recorder
    from jarvis.voice.loop import VOICE_INSTRUCTIONS, VoiceLoop
    from jarvis.voice.push_to_talk import HotkeyListener
    from jarvis.voice.stt.base import STTError
    from jarvis.voice.stt.factory import create_stt
    from jarvis.voice.tts.base import TTSError
    from jarvis.voice.tts.factory import create_speaker

    try:
        voice = load_voice_config()
        setup_logging(config.logs_dir, secrets=(*config.secrets, *voice.secrets))
        print(f"Loading speech model {voice.stt_model}...")
        stt = create_stt(voice)
        speaker = create_speaker(voice, purpose="reply")
        hotkey = HotkeyListener(voice.ptt_key)
    except (ConfigError, STTError, TTSError, AudioError, ValueError) as e:
        print(f"Voice mode unavailable: {e}\nFalling back to text chat.\n")
        logger.warning("Voice mode unavailable: %s", e)
        return False

    session = ChatSession(llm, history_turns=config.history_turns, instructions=VOICE_INSTRUCTIONS,
                          tools=build_tools())
    loop = VoiceLoop(session, stt, speaker, Recorder(voice.max_record_seconds), hotkey.events,
                     hotkey.drain, voice.max_record_seconds)
    hotkey.start()
    print(f"Jarvis voice mode ({config.llm_model}, {voice.stt_model}). "
          f"Hold {voice.ptt_key.upper()} and speak; release to send. Ctrl+C quits.")
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        hotkey.stop()
    return True


def run_text(config: Config, session: ChatSession) -> None:
    print(f"Jarvis ({config.llm_model}). {HELP}")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        if text == "/reset":
            session.reset()
            print("(history cleared)")
            continue
        if text.startswith("/"):
            print(HELP)
            continue

        try:
            reply = session.ask(text, confirmer=TextConfirmer())
        except LLMError as e:
            print(f"[error] {e}")
            continue
        except Exception:
            logger.exception("Unexpected error during chat")
            print("[error] Something went wrong. Details are in logs/jarvis.log.")
            continue

        print(f"jarvis> {reply.text or '(no reply text)'}")
        if reply.incomplete_reason:
            print(f"  (reply cut short: {reply.incomplete_reason})")
        if reply.usage:
            print(f"  [{reply.usage.input_tokens} in / {reply.usage.output_tokens} out tokens]")


if __name__ == "__main__":
    sys.exit(main())
