"""`jarvis` command: text chat, or `jarvis --voice` for push-to-talk. print() is allowed here (CLI UI only)."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any
import queue
import sys
import threading

from jarvis.core.text import for_terminal
from jarvis.core.chat import ChatSession
from jarvis.core.config import PROJECT_ROOT, Config, ConfigError, load_config
from jarvis.core.instance import InstanceLock
from jarvis.core.logging_setup import setup_logging
from jarvis.core.scheduler import ReminderScheduler
from jarvis.integrations.credentials import default_store
from jarvis.llm.base import LLMAdapter, LLMError
from jarvis.llm.factory import create_embedder, create_llm
from jarvis.memory.store import MemoryStore
from jarvis.memory.tools import memory_tools
from jarvis.tools.files import FileIndex, file_tools
from jarvis.tools.briefing import Briefer, Briefing, DailyJob, JobState, briefing_tool
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.search.base import SearchAdapter, SearchError
from jarvis.tools.search.factory import create_search
from jarvis.tools.web_search import Sources, format_sources, web_search_tool
from jarvis.tools.reminders import Reminder, ReminderStore, describe_when, local_now, reminder_tools

logger = logging.getLogger(__name__)

HELP = "Commands: /reset clears history, /exit quits."


class TextConfirmer:
    """Asks in the terminal before a CONFIRM-tier action. Only y/yes approves; anything else is no."""

    def confirm(self, question: str) -> bool:
        try:
            answer = input(f"jarvis> {for_terminal(question)} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")


@dataclass
class Services:
    """Everything the tools need, created once at startup."""

    store: ReminderStore
    files: FileIndex | None
    search: SearchAdapter | None
    sources: Sources  # web sources of the latest answer, shown on screen
    memory: MemoryStore  # facts the user asked Jarvis to remember
    briefer: Briefer  # morning briefing (Gmail + today's reminders)

    def close(self) -> None:
        for store in (self.store, self.memory, self.files):
            if store is not None:
                store.close()

    def build_tools(self) -> ToolRegistry:
        """All tools Jarvis may use. Each declares its permission tier."""
        tools = [*reminder_tools(self.store), *memory_tools(self.memory), briefing_tool(self.briefer),
                 *(file_tools(self.files) if self.files else [])]
        if self.search is not None:
            tools.append(web_search_tool(self.search, self.sources))
        return ToolRegistry(tools)


def start_search(config: Config) -> SearchAdapter | None:
    if not config.search_provider:
        return None
    try:
        return create_search(config.search_provider, config.search_api_key)
    except SearchError as e:
        print(f"Web search unavailable: {e}")
        logger.warning("Web search unavailable: %s", e)
        return None


def start_file_index(config: Config) -> FileIndex | None:
    """Open the file index and bring it up to date in the background (startup is not delayed)."""
    if not config.file_search_dirs:
        return None
    # The Jarvis project (which holds .env with the API keys) is never indexed, even inside an approved folder.
    index = FileIndex(config.data_dir / "files.db", config.file_search_dirs, excluded=(PROJECT_ROOT, config.data_dir))

    def refresh() -> None:
        try:
            index.refresh()
        except Exception:
            logger.exception("File index refresh failed")

    threading.Thread(target=refresh, name="file-index", daemon=True).start()
    return index


def gmail_client(config: Config) -> Any:
    """The Gmail reader, or None until Google is set up (client in Credential Manager or the client file)."""
    if not (default_store().get("GOOGLE_CLIENT_ID") or config.gmail_client_file.is_file()):
        return None
    from jarvis.integrations.gmail import GmailClient  # loads Google's libraries only when set up

    return GmailClient(config.gmail_client_file, config.gmail_token_file)


def gmail_login(config: Config) -> int:
    from jarvis.integrations.gmail import GmailClient, GmailError

    print("Opening your browser for Google sign-in (read-only Gmail access)...")
    try:
        GmailClient(config.gmail_client_file, config.gmail_token_file).login()
    except GmailError as e:
        print(f"Gmail sign-in failed: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # e.g. the browser window was closed
        logger.error("Gmail sign-in failed: %s", type(e).__name__)
        print(f"Gmail sign-in did not complete ({type(e).__name__}).", file=sys.stderr)
        return 1
    print("Signed in. The Gmail sign-in is saved in Windows Credential Manager.")
    return 0


class ReminderAlerts:
    """Announces due reminders: desktop notification, plus the terminal (text mode) or voice (voice mode)."""

    def __init__(self) -> None:
        self.voice_queue: queue.Queue[Any] | None = None  # set when voice mode starts
        # WindowsNotifier loads its native libraries on the first notification, after the speech
        # model (loading them earlier crashes faster-whisper; see integrations/notify.py).
        from jarvis.integrations.notify import WindowsNotifier

        self._notifier = WindowsNotifier()

    def __call__(self, reminder: Reminder, missed: bool) -> None:
        message = reminder.text
        if missed:
            message = f"Missed reminder from {describe_when(reminder.due, local_now())}: {reminder.text}"
        self._notifier.notify("Jarvis reminder", message)
        if self.voice_queue is not None:
            self.voice_queue.put(f"Reminder: {message}" if not missed else message)
        else:
            print(f"\n[reminder] {for_terminal(message)}\nyou> ", end="", flush=True)

    def briefing(self, briefing: Briefing) -> None:
        self._notifier.notify("Jarvis briefing", "Your morning briefing is ready.")
        if self.voice_queue is not None:
            from jarvis.voice.loop import BriefingAlert

            self.voice_queue.put(BriefingAlert(briefing.spoken, briefing.screen))
        else:
            print(f"\n[briefing]\n{briefing.screen}\njarvis> {briefing.spoken}\nyou> ", end="", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal assistant.")
    parser.add_argument("--voice", action="store_true", help="push-to-talk voice mode (falls back to text)")
    parser.add_argument("--gmail-login", action="store_true", help="sign in to Gmail (read-only) and exit")
    parser.add_argument("--secrets", choices=("status", "import", "set"),
                        help="manage secrets in Windows Credential Manager and exit")
    parser.add_argument("--autostart", choices=("on", "off", "status"),
                        help="start Jarvis (voice, minimized) when you sign in to Windows, and exit")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    setup_logging(config.logs_dir, secrets=config.secrets)
    logger.info("Jarvis starting: %s", config)
    if args.secrets:
        from jarvis.core import secrets_cli

        return secrets_cli.run(args.secrets, config)
    if args.gmail_login:
        return gmail_login(config)
    if args.autostart:
        return autostart_command(args.autostart, config)

    # One Jarvis at a time: a second copy would also answer the talk key and repeat alerts.
    lock = InstanceLock(config.data_dir / "jarvis.lock")
    if not lock.acquire():
        print("Jarvis is already running (look for its window on the taskbar).")
        logger.info("Second Jarvis not started: another one is running")
        return 0  # not an error: the auto-start launcher must not restart it
    try:
        return run_jarvis(config, args.voice)
    finally:
        lock.release()


def autostart_command(action: str, config: Config) -> int:
    from jarvis.integrations import autostart

    if action == "on":
        try:
            entry = autostart.enable(config.project_root)
        except (FileNotFoundError, FileExistsError) as e:
            print(f"Auto-start not enabled: {e}", file=sys.stderr)
            return 1
        print(f"Auto-start is on: Jarvis voice mode starts minimized when you sign in.\n  entry: {entry}")
    elif action == "off":
        print("Auto-start is off." if autostart.disable() else "Auto-start was not on.")
    else:
        print(f"Auto-start: {autostart.status(config.project_root)}")
    return 0


def run_jarvis(config: Config, voice: bool) -> int:
    try:
        llm = create_llm(config)
    except LLMError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    store = ReminderStore(config.data_dir / "jarvis.db")
    gmail = gmail_client(config)
    services = Services(store, start_file_index(config), start_search(config), Sources(),
                        MemoryStore(config.data_dir / "memory.db", create_embedder(config)),
                        Briefer(gmail, store, llm))
    alerts = ReminderAlerts()
    jobs = []
    # Scheduled only once Gmail is set up (otherwise it would just say so every morning); on demand always works.
    if config.briefing_time is not None and gmail is not None:
        jobs.append(DailyJob("morning_briefing", config.briefing_time,
                             lambda: alerts.briefing(services.briefer.compose()), JobState(config.data_dir / "jarvis.db")))
    scheduler = ReminderScheduler(services.store, alerts, daily_jobs=jobs)
    try:
        if not (voice and run_voice(config, llm, services, alerts, scheduler)):
            scheduler.start()
            session = ChatSession(llm, history_turns=config.history_turns, tools=services.build_tools(),
                                  memory=services.memory)
            run_text(config, session, services.sources)
    finally:
        scheduler.stop()
        services.close()  # release the database files (Windows keeps open files locked)
    logger.info("Jarvis stopped")
    return 0


def run_voice(config: Config, llm: LLMAdapter, services: Services, alerts: ReminderAlerts,
              scheduler: ReminderScheduler) -> bool:
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
        if voice.stt_provider == "openai":
            fallback = f", local fallback {voice.stt_fallback_model}" if voice.stt_fallback_model else ""
            print(f"Speech recognition: {voice.stt_model} (cloud){fallback}")
        else:
            print(f"Loading speech model {voice.stt_model}...")
        stt = create_stt(voice, api_key=config.llm_api_key)
        speaker = create_speaker(voice, purpose="reply")
        briefing_speaker = create_speaker(voice, purpose="briefing")  # ElevenLabs where JARVIS_ELEVENLABS_USE_FOR allows
        hotkey = HotkeyListener(voice.ptt_key)
    except (ConfigError, STTError, TTSError, AudioError, ValueError) as e:
        print(f"Voice mode unavailable: {e}\nFalling back to text chat.\n")
        logger.warning("Voice mode unavailable: %s", e)
        return False

    session = ChatSession(llm, history_turns=config.history_turns, instructions=VOICE_INSTRUCTIONS,
                          tools=services.build_tools(), memory=services.memory)
    alerts.voice_queue = queue.Queue()
    loop = VoiceLoop(session, stt, speaker, Recorder(voice.max_record_seconds), hotkey.events,
                     hotkey.drain, voice.max_record_seconds, alerts=alerts.voice_queue,
                     sources=services.sources, briefing_speaker=briefing_speaker)
    hotkey.start()
    scheduler.start()
    print(f"Jarvis voice mode ({config.llm_model}, {voice.stt_model}). "
          f"Hold {voice.ptt_key.upper()} and speak; release to send. Ctrl+C quits.")
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        hotkey.stop()
    return True


def run_text(config: Config, session: ChatSession, sources: Sources) -> None:
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

        print(f"jarvis> {for_terminal(reply.text) or '(no reply text)'}")
        if found := sources.take():
            print(format_sources(found))
        if reply.incomplete_reason:
            print(f"  (reply cut short: {reply.incomplete_reason})")
        if reply.usage:
            print(f"  [{reply.usage.input_tokens} in / {reply.usage.output_tokens} out tokens]")


if __name__ == "__main__":
    sys.exit(main())
