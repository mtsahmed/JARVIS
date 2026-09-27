"""Text chat UI: `jarvis` command. print() is allowed here (CLI UI only)."""

from __future__ import annotations

import logging
import sys

from jarvis.core.chat import ChatSession
from jarvis.core.config import ConfigError, load_config
from jarvis.core.logging_setup import setup_logging
from jarvis.llm.base import LLMError
from jarvis.llm.factory import create_llm

logger = logging.getLogger(__name__)

HELP = "Commands: /reset clears history, /exit quits."


def main() -> int:
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
    session = ChatSession(llm, history_turns=config.history_turns)

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
            reply = session.ask(text)
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

    logger.info("Jarvis stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
