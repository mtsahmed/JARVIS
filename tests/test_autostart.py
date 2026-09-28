"""Phase 7A: Startup-folder entry, the self-restarting launcher, and one-Jarvis-at-a-time."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis.core.config import PROJECT_ROOT
from jarvis.core.instance import InstanceLock
from jarvis.integrations import autostart

LAUNCHER = PROJECT_ROOT / "scripts" / "run_jarvis.cmd"


# --- Startup-folder entry --------------------------------------------------------------------------


def test_enable_status_disable(tmp_path: Path) -> None:
    assert autostart.status(PROJECT_ROOT, tmp_path) == "off"
    entry = autostart.enable(PROJECT_ROOT, tmp_path)
    raw = entry.read_bytes().decode("utf-8")  # raw bytes: read_text() would turn \r\n into \n
    assert f'start "Jarvis" /min "{LAUNCHER}"' in raw and "\r\n" in raw
    assert autostart.status(PROJECT_ROOT, tmp_path) == "on"
    autostart.enable(PROJECT_ROOT, tmp_path)  # idempotent
    assert autostart.disable(tmp_path) and not entry.exists()
    assert not autostart.disable(tmp_path)  # already off


def test_never_overwrites_or_removes_a_foreign_file(tmp_path: Path) -> None:
    foreign = tmp_path / "Jarvis.cmd"
    foreign.write_text("@echo off\necho someone else's script\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        autostart.enable(PROJECT_ROOT, tmp_path)
    assert not autostart.disable(tmp_path) and foreign.exists()
    assert "not created by Jarvis" in autostart.status(PROJECT_ROOT, tmp_path)


def test_entry_for_another_jarvis_folder_is_reported(tmp_path: Path) -> None:
    autostart.enable(PROJECT_ROOT, tmp_path)
    assert autostart.status(tmp_path / "other-copy", tmp_path) == "on, but pointing to another Jarvis folder"


def test_cli_uses_the_users_startup_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert autostart.startup_dir() == tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


# --- the launcher ------------------------------------------------------------------------------------


def test_launcher_text() -> None:
    data = LAUNCHER.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n")  # Windows line endings throughout
    text = data.decode()
    assert '".venv\\Scripts\\jarvis.exe" --voice' in text
    assert 'if "%CODE%"=="0" goto end' in text  # a normal exit is never restarted
    assert "if %RESTARTS% GEQ 5 goto giveup" in text  # bounded restarts
    assert '"%SystemRoot%\\System32\\timeout.exe"' in text  # Windows timeout, not Git's


def run_launcher_with_stub(tmp_path: Path, exit_codes: list[int]) -> tuple[str, list[str]]:
    """Run the real launcher logic with Jarvis replaced by a stub that exits with the given codes in turn."""
    (tmp_path / "scripts").mkdir()
    calls = tmp_path / "calls.txt"
    # Stand-in for jarvis.exe: records each run and exits with the next code from the list (then 0).
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"calls = Path(r'{calls}')\ncodes = {exit_codes!r}\n"
        "n = len(calls.read_text().split()) if calls.exists() else 0\n"
        "calls.write_text((calls.read_text() if calls.exists() else '') + 'run\\n')\n"
        "sys.exit(codes[n] if n < len(codes) else 0)\n",
        encoding="utf-8",
    )
    text = LAUNCHER.read_text(encoding="utf-8")
    text = text.replace('".venv\\Scripts\\jarvis.exe" --voice', f'"{sys.executable}" "{stub}"')
    text = text.replace("/t 10", "/t 1").replace("pause", "rem pause")
    (tmp_path / "scripts" / "run_jarvis.cmd").write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode())
    result = subprocess.run(["cmd", "/c", str(tmp_path / "scripts" / "run_jarvis.cmd")], capture_output=True,
                            text=True, timeout=60, stdin=subprocess.DEVNULL)
    return result.stdout, calls.read_text().split()


def test_crashes_are_restarted_and_a_clean_exit_stops(tmp_path: Path) -> None:
    out, calls = run_launcher_with_stub(tmp_path, [3, 1, 0])  # crash, crash, then normal exit
    assert len(calls) == 3
    assert "Restart 1 of 4" in out and "Restart 2 of 4" in out and "not restarting" not in out


def test_restarts_are_bounded(tmp_path: Path) -> None:
    out, calls = run_launcher_with_stub(tmp_path, [1, 1, 1, 1, 1, 1, 1])  # keeps crashing
    assert len(calls) == 5 and "stopped unexpectedly 5 times; not restarting" in out


# --- one Jarvis at a time ------------------------------------------------------------------------------

HOLDER = """
import sys, time
from pathlib import Path
from jarvis.core.instance import InstanceLock
lock = InstanceLock(Path(sys.argv[1]))
print("held" if lock.acquire() else "busy", flush=True)
time.sleep(30)
"""


def test_second_instance_is_refused_until_the_first_ends(tmp_path: Path) -> None:
    lock_file = tmp_path / "jarvis.lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock_file)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        assert not InstanceLock(lock_file).acquire()  # the other Jarvis is running
    finally:
        holder.kill()  # simulates a crash: Windows releases the lock with the process
        holder.wait(timeout=10)
    lock = InstanceLock(lock_file)
    assert lock.acquire()
    lock.release()


def test_lock_can_be_taken_again_after_release(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "jarvis.lock")
    assert first.acquire()
    first.release()
    again = InstanceLock(tmp_path / "jarvis.lock")
    assert again.acquire()
    again.release()


def test_main_exits_cleanly_when_already_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    import jarvis.cli as cli
    from jarvis.core.config import _KEYS, load_config

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-x\nJARVIS_LLM_PROVIDER=openai\nJARVIS_LLM_MODEL=m\n"
                                   "JARVIS_MAX_OUTPUT_TOKENS=1\nJARVIS_LLM_TIMEOUT_SECONDS=1\nJARVIS_HISTORY_TURNS=1\n",
                                   encoding="utf-8")
    config = load_config(project_root=tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli.InstanceLock, "acquire", lambda self: False)
    monkeypatch.setattr("sys.argv", ["jarvis", "--voice"])
    assert cli.main() == 0  # exit code 0: the launcher does not treat it as a crash
    assert "already running" in capsys.readouterr().out
