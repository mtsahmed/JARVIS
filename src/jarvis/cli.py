"""`jarvis` command: text chat, or `jarvis --voice` for push-to-talk. print() is allowed here (CLI UI only)."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time
from typing import Any
import queue
import sys
import threading

from jarvis.core.text import for_terminal
from jarvis.core.chat import ChatSession
from jarvis.core.config import PROJECT_ROOT, Config, ConfigError, load_config
from jarvis.core.approvals import TEXT_TIMEOUT_SECONDS, Approvals
from jarvis.core.events import EventBus
from jarvis.core.inbox import END, Inbox, Typed
from jarvis.core.instance import InstanceLock
from jarvis.core import backup, health, usage_report
from jarvis.core.usage_report import CostMeter, Prices
from jarvis.core.health import FailureWatcher, start_background_check
from jarvis.core.logging_setup import add_usage_observer, setup_logging
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


EXIT_COMMANDS = ("/exit", "/quit")


class TerminalReader:
    """Text mode: reads terminal lines on a background thread into the Inbox, so the terminal and the
    HUD's text box can both send requests. It reads one line each time it is asked (so its prompt
    never lands in the middle of a reply) and stops after /exit or the end of input."""

    def __init__(self, inbox: Inbox) -> None:
        self._inbox = inbox
        self._wanted = threading.Event()
        self._prompt = ""
        self.reading = False
        threading.Thread(target=self._run, name="terminal-input", daemon=True).start()

    def request(self, prompt: str) -> None:
        """Read the next line (unless a read is already waiting for the user)."""
        if not self.reading:
            self._prompt = prompt
            self._wanted.set()

    def _run(self) -> None:
        while True:
            self._wanted.wait()
            self.reading = True
            self._wanted.clear()
            try:
                line = input(self._prompt)
            except (EOFError, KeyboardInterrupt):
                self._inbox.queue.put(END)
                return
            finally:
                self.reading = False
            self._inbox.queue.put(Typed(line, "terminal"))
            if line.strip() in EXIT_COMMANDS:
                return


def next_typed(inbox: Inbox, give_up: Callable[[], bool] = lambda: False) -> Typed | None:
    """The next typed request (terminal or HUD), or None at the end of input or once `give_up()` is
    true. Talk-key events are ignored: text mode has no microphone."""
    while not give_up():
        try:
            item = inbox.queue.get(timeout=0.2)  # timeout keeps Ctrl+C responsive on Windows
        except queue.Empty:
            continue
        if item == END:
            inbox.queue.put(END)  # also ends the main loop
            return None
        if isinstance(item, Typed):
            return item
    return None


class TextConfirmer:
    """Asks before a CONFIRM-tier action. The first answer wins: typed in the terminal or on the HUD, or
    clicked on the HUD's dialog. Only y/yes approves; anything else, the end of input, or no answer
    within TEXT_TIMEOUT_SECONDS is no."""

    def __init__(self, inbox: Inbox, reader: TerminalReader, approvals: Approvals | None = None,
                 bus: EventBus | None = None) -> None:
        self._inbox, self._reader = inbox, reader
        self._bus = bus or EventBus()
        self._approvals = approvals or Approvals(self._bus)

    def confirm(self, question: str, action: str = "") -> bool:
        approval = self._approvals.open(question, action, timeout=TEXT_TIMEOUT_SECONDS)
        self._bus.state("confirm", question)
        print(f"jarvis> {for_terminal(question)} [y/N]")
        self._reader.request("  your answer> ")
        answer = next_typed(self._inbox, give_up=lambda: approval.decided or not approval.seconds_left())
        if answer is not None:
            if answer.source == "hud":
                print(f"  (typed on the HUD) {for_terminal(answer.text)}")
            by = "screen" if answer.source == "hud" else "terminal"
            self._approvals.answer(approval.request_id, answer.text.strip().lower() in ("y", "yes"), by)
        self._approvals.expire(approval)  # no answer (or end of input): no; a no-op if it was answered
        if approval.by == "screen" and answer is None:
            print(f"  ({'approved' if approval.approved else 'declined'} on the HUD)")
        elif approval.by == "timeout":
            print(f"  (no answer in {TEXT_TIMEOUT_SECONDS:.0f} s: not done)")
        self._bus.state("thinking")
        return approval.approved


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

    def problem(self, message: str) -> None:
        """Something needs the user's attention (failed health check, repeated failures)."""
        self._notifier.notify("Jarvis needs attention", message)
        if self.voice_queue is not None:
            self.voice_queue.put(f"Heads up: {message}")
        else:
            print(f"\n[attention] {for_terminal(message)}\nyou> ", end="", flush=True)

    def problems(self, checks: list[Any]) -> None:
        for check in checks[:2]:  # at most two, so startup is not flooded
            self.problem(f"{check.name}: {check.detail}")

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
    parser.add_argument("--health", action="store_true", help="check every part of Jarvis (free) and exit")
    parser.add_argument("--backup", action="store_true", help="back up memories and reminders now, and exit")
    parser.add_argument("--usage", action="store_true", help="show this month's usage and budget, and exit")
    parser.add_argument("--restore", nargs="?", const="list", metavar="YYYY-MM-DD",
                        help="list backups, or restore the one from that date (Jarvis must be stopped), and exit")
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
    if args.health:
        return health_command(config)
    if args.backup:
        return backup_command(config)
    if args.usage:
        return usage_command(config)
    if args.restore:
        return restore_command(config, args.restore)

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


HARD_CAP_NOTE = "Hard cap: the spending limit set in your OpenAI dashboard (owner: $20)."


def cost_meter(config: Config, alert: Callable[[str], None]) -> CostMeter:
    logs = [config.logs_dir / "usage.jsonl", *(config.logs_dir / f"usage.jsonl.{i}" for i in (1, 2, 3))]
    return CostMeter(config.data_dir / "jarvis.db", Prices.load(config.prices_file), config.monthly_budget_usd,
                     config.budget_warn_percent, alert, usage_logs=logs)


def allowance_checks(config: Config, search: Any) -> list[tuple[str, Any]]:
    """Free allowances with their own usage APIs: Tavily credits, ElevenLabs characters (free calls)."""
    checks: list[tuple[str, Any]] = []
    if search is not None and hasattr(search, "usage"):
        checks.append(("Web search credits (Tavily)", search.usage))
    try:
        from jarvis.core.config import load_voice_config

        voice = load_voice_config()
        if voice.elevenlabs_use_for and voice.elevenlabs_api_key:
            from jarvis.voice.tts.elevenlabs_adapter import ElevenLabsAdapter

            adapter = ElevenLabsAdapter(voice.elevenlabs_api_key, voice.elevenlabs_voice_id or "", voice.elevenlabs_model or "")
            checks.append(("Premium voice characters (ElevenLabs)", adapter.quota))
    except ConfigError:
        pass
    return checks


def usage_command(config: Config) -> int:
    meter = cost_meter(config, alert=lambda message: None)  # the report itself shows the numbers
    try:
        print(usage_report.report(meter, config.monthly_budget_usd, allowance_checks(config, start_search(config)),
                                  HARD_CAP_NOTE))
    finally:
        meter.close()
    return 0


def backup_command(config: Config) -> int:
    """Safe while Jarvis is running (SQLite online backup)."""
    try:
        folder = backup.backup_now(config.data_dir, local_now().date())
    except backup.BackupError as e:
        print(f"Backup failed: {e}", file=sys.stderr)
        return 1
    JobState(config.data_dir / "jarvis.db").mark("backup", local_now().date())
    print(f"Backup saved: {folder}")
    return 0


def restore_command(config: Config, when: str, ask: Callable[[str], str] = input) -> int:
    days = backup.list_backups(config.data_dir)
    if when == "list":
        print("Backups: " + (", ".join(d.isoformat() for d in days) if days else "none yet"))
        print("Restore one with: jarvis --restore YYYY-MM-DD")
        return 0
    try:
        day = date.fromisoformat(when)
    except ValueError:
        print(f"Use a date like {local_now():%Y-%m-%d}.", file=sys.stderr)
        return 1
    lock = InstanceLock(config.data_dir / "jarvis.lock")
    if not lock.acquire():  # replacing databases under a running Jarvis would corrupt its view of them
        print("Jarvis is running. Stop it first (Ctrl+C in its window), then restore.", file=sys.stderr)
        return 1
    try:
        answer = ask(f"Replace your current memories and reminders with the backup from {day}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 0
        try:
            safety = backup.restore(config.data_dir, day)
        except backup.BackupError as e:
            print(f"Restore failed, nothing changed: {e}", file=sys.stderr)
            return 1
        print(f"Restored {day}. Your previous data was saved in {safety}.")
        return 0
    finally:
        lock.release()


def health_checks(config: Config, llm: Any, search: Any, gmail: Any, microphone: bool) -> list[tuple[str, Any]]:
    """Every check is free (no chat tokens, search credits or voice characters)."""
    from jarvis.integrations import autostart

    def elevenlabs() -> Any:
        from jarvis.core.config import load_voice_config

        voice = load_voice_config()
        if not voice.elevenlabs_use_for:
            return health.check_elevenlabs(None)
        from jarvis.voice.tts.elevenlabs_adapter import ElevenLabsAdapter

        adapter = ElevenLabsAdapter(voice.elevenlabs_api_key or "", voice.elevenlabs_voice_id or "", voice.elevenlabs_model or "")
        return health.check_elevenlabs(adapter.characters_left)

    def briefing() -> Any:
        last = JobState(config.data_dir / "jarvis.db").last_run("morning_briefing")
        expected = config.briefing_time is not None and gmail is not None
        return health.check_daily_job("morning_briefing", "Morning briefing", last, expected, local_now().date())

    checks = [
        ("OpenAI", lambda: health.check_openai(llm)),
        ("Web search", lambda: health.check_search(search)),
        ("Premium voice", elevenlabs),
        ("Gmail", lambda: health.check_gmail(gmail)),
        ("Disk space", lambda: health.check_disk(config.project_root)),
        ("Morning briefing", briefing),
        ("Backups", lambda: health.check_daily_job(
            "backup", "Backups", JobState(config.data_dir / "jarvis.db").last_run("backup"), True, local_now().date())),
        ("Auto-start", lambda: health.check_autostart(autostart.status(config.project_root))),
    ]
    if microphone:
        checks.insert(4, ("Microphone", health.check_microphone))
    return checks


def health_command(config: Config) -> int:
    try:
        llm = create_llm(config)
    except LLMError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    results = health.run_checks(health_checks(config, llm, start_search(config), gmail_client(config), microphone=True))
    print(health.report(results))
    return 1 if any(c.status == "fail" for c in results) else 0


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
    add_usage_observer(FailureWatcher(alerts.problem).observe)  # 3 failures in a row of one service -> alert
    meter = cost_meter(config, alerts.problem)  # estimated monthly OpenAI spend -> budget alerts (80%, 100%)
    add_usage_observer(meter.observe)

    def daily_money_check() -> None:
        meter.check_budget()  # repeats the budget warning once a day while over the warning level
        for warning in usage_report.allowance_warnings(allowance_checks(config, services.search),
                                                       config.budget_warn_percent):
            alerts.problem(warning)

    # Daily jobs run at the first check each day (time 00:00 = "as soon as possible").
    jobs = [DailyJob("backup", time(0, 0), lambda: backup.backup_now(config.data_dir, local_now().date()),
                     JobState(config.data_dir / "jarvis.db")),
            DailyJob("money_check", time(0, 0), daily_money_check, JobState(config.data_dir / "jarvis.db"))]
    # Scheduled only once Gmail is set up (otherwise it would just say so every morning); on demand always works.
    if config.briefing_time is not None and gmail is not None:
        jobs.append(DailyJob("morning_briefing", config.briefing_time,
                             lambda: alerts.briefing(services.briefer.compose()), JobState(config.data_dir / "jarvis.db")))
    scheduler = ReminderScheduler(services.store, alerts, daily_jobs=jobs)

    def check_health() -> None:  # started once Jarvis is ready, so voice mode can speak a problem
        start_background_check(health_checks(config, llm, services.search, gmail, microphone=voice), alerts.problems)

    bus = EventBus()  # what Jarvis is doing, for displays such as the HUD
    inbox = Inbox()  # talk-key events and typed requests (terminal, key, HUD)
    approvals = Approvals(bus)  # CONFIRM-tier approvals: by voice, typing, or the HUD
    hud = start_hud(config, bus, inbox, approvals)
    try:
        if not (voice and run_voice(config, llm, services, alerts, scheduler, on_started=check_health, bus=bus,
                                    inbox=inbox, approvals=approvals)):
            scheduler.start()
            check_health()
            session = ChatSession(llm, history_turns=config.history_turns, tools=services.build_tools(),
                                  memory=services.memory)
            run_text(config, session, services.sources, bus, inbox, approvals)
    finally:
        scheduler.stop()
        services.close()  # release the database files (Windows keeps open files locked)
        meter.close()
        if hud is not None:
            hud.stop()
    logger.info("Jarvis stopped")
    return 0


def start_hud(config: Config, bus: EventBus, inbox: Inbox | None = None, approvals: Approvals | None = None) -> Any:
    """Start the local HUD page if JARVIS_UI_ENABLED. A HUD that can't start never stops Jarvis."""
    if not config.ui_enabled:
        return None
    from jarvis.ui.server import HudServer, UIError  # only loads websockets when the HUD is on

    hud = HudServer(bus, config.ui_port, inbox=inbox, approvals=approvals)
    try:
        hud.start()
    except UIError as e:
        print(f"HUD unavailable: {e}. Jarvis runs without it.")
        logger.warning("HUD unavailable: %s", e)
        return None
    print(f"HUD: {hud.url}")
    return hud


def run_voice(config: Config, llm: LLMAdapter, services: Services, alerts: ReminderAlerts,
              scheduler: ReminderScheduler, on_started: Callable[[], None] = lambda: None,
              bus: EventBus | None = None, inbox: Inbox | None = None,
              approvals: Approvals | None = None) -> bool:
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
        inbox = inbox or Inbox()
        hotkey = HotkeyListener(voice.ptt_key, events=inbox.queue)
    except (ConfigError, STTError, TTSError, AudioError, ValueError) as e:
        print(f"Voice mode unavailable: {e}\nFalling back to text chat.\n")
        logger.warning("Voice mode unavailable: %s", e)
        return False

    session = ChatSession(llm, history_turns=config.history_turns, instructions=VOICE_INSTRUCTIONS,
                          tools=services.build_tools(), memory=services.memory)
    alerts.voice_queue = queue.Queue()
    loop = VoiceLoop(session, stt, speaker, Recorder(voice.max_record_seconds), inbox.queue,
                     voice.max_record_seconds, alerts=alerts.voice_queue,
                     sources=services.sources, briefing_speaker=briefing_speaker, bus=bus, approvals=approvals)
    hotkey.start()
    inbox.voice = True  # the HUD's mic button works from now on
    scheduler.start()
    on_started()
    print(f"Jarvis voice mode ({config.llm_model}, {voice.stt_model}). "
          f"Hold {voice.ptt_key.upper()} and speak; release to send. Ctrl+C quits.")
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        inbox.voice = False
        hotkey.stop()
    return True


def run_text(config: Config, session: ChatSession, sources: Sources, bus: EventBus | None = None,
             inbox: Inbox | None = None, approvals: Approvals | None = None) -> None:
    """Text chat. Requests come from the terminal or the HUD's text box (the Inbox). Commands like
    /exit work only from the terminal: the HUD cannot stop Jarvis."""
    bus, inbox = bus or EventBus(), inbox or Inbox()
    reader = TerminalReader(inbox)
    confirmer = TextConfirmer(inbox, reader, approvals or Approvals(bus), bus)
    print(f"Jarvis ({config.llm_model}). {HELP}")
    bus.state("idle")
    try:
        while True:
            reader.request("\nyou> ")
            item = next_typed(inbox)
            if item is None:
                print()
                break
            text = item.text.strip()
            if item.source == "hud":
                print(f"\nyou (HUD)> {for_terminal(text)}")
            elif text in EXIT_COMMANDS:
                break
            elif text == "/reset":
                session.reset()
                print("(history cleared)")
                continue
            elif text.startswith("/"):
                print(HELP)
                continue
            if text:
                _text_turn(text, session, sources, bus, confirmer)
    except KeyboardInterrupt:
        print()


def _text_turn(text: str, session: ChatSession, sources: Sources, bus: EventBus, confirmer: TextConfirmer) -> None:
    bus.transcript(text)
    bus.state("thinking")
    try:
        reply = session.ask(text, confirmer=confirmer)
    except LLMError as e:
        print(f"[error] {e}")
        bus.error("language_service", str(e))
        bus.state("idle")
        return
    except Exception:
        logger.exception("Unexpected error during chat")
        print("[error] Something went wrong. Details are in logs/jarvis.log.")
        bus.error("internal", "Something went wrong. Details are in the log.")
        bus.state("idle")
        return

    print(f"jarvis> {for_terminal(reply.text) or '(no reply text)'}")
    found = sources.take()
    if found:
        print(format_sources(found))
    if reply.text:
        bus.reply(reply.text, [{"title": r.title, "url": r.url} for r in found])
    bus.state("idle")
    if reply.incomplete_reason:
        print(f"  (reply cut short: {reply.incomplete_reason})")
    if reply.usage:
        print(f"  [{reply.usage.input_tokens} in / {reply.usage.output_tokens} out tokens]")


if __name__ == "__main__":
    sys.exit(main())
