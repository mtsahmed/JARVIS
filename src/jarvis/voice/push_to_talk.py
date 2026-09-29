"""Global hold-to-talk hotkey (pynput). The only module that imports `pynput`.

Privacy: the listener sees every key event system-wide, but only compares it with the
configured key. Keys are never logged, stored, or passed on.
"""

from __future__ import annotations

import queue
from typing import Literal

from pynput import keyboard

PTTEvent = Literal["start", "stop"]


class PushToTalk:
    """Pure state machine: press -> "start", release -> "stop". Key auto-repeat is ignored."""

    def __init__(self) -> None:
        self._held = False

    def press(self) -> PTTEvent | None:
        if self._held:
            return None  # Windows sends repeated key-down events while a key is held
        self._held = True
        return "start"

    def release(self) -> PTTEvent | None:
        if not self._held:
            return None
        self._held = False
        return "stop"


def parse_key(name: str) -> keyboard.Key | keyboard.KeyCode:
    """'f9' -> Key.f9; a single character like 'j' -> KeyCode for that character."""
    name = name.strip().lower()
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    key = getattr(keyboard.Key, name, None)
    if not isinstance(key, keyboard.Key):
        raise ValueError(f"Unknown push-to-talk key {name!r}. Use a name like f9, f10, or a single letter.")
    return key


class HotkeyListener:
    """Feeds "start"/"stop" events for one key into a queue from pynput's listener thread."""

    def __init__(self, key_name: str, events: queue.Queue | None = None) -> None:
        self._key = parse_key(key_name)
        self._ptt = PushToTalk()
        self.events: queue.Queue = events if events is not None else queue.Queue()  # e.g. the Inbox's queue
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)

    # Callbacks run on pynput's thread and must return quickly (they delay input system-wide).
    def _on_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key == self._key and (event := self._ptt.press()):
            self.events.put(event)

    def _on_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key == self._key and (event := self._ptt.release()):
            self.events.put(event)

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        self._listener.stop()


def _key_test(seconds: float = 20.0) -> None:
    """Report presses of the configured key, and only a COUNT of other keys (never which ones)."""
    import time

    from jarvis.core.config import load_voice_config

    name = load_voice_config().ptt_key
    target = parse_key(name)
    other = 0

    ptt = PushToTalk()  # same auto-repeat filtering as voice mode

    def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
        nonlocal other
        if key != target:
            other += 1
        elif ptt.press():
            print(f"  {name}: DOWN (would start recording)", flush=True)

    def on_release(key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key == target and ptt.release():
            print(f"  {name}: UP (would stop recording)", flush=True)

    print(f"Key test for {seconds:g}s: hold and release {name.upper()} a few times, and type any other key once.")
    with keyboard.Listener(on_press=on_press, on_release=on_release):
        time.sleep(seconds)
    print(f"Done. Other key presses seen: {other} (0 means no keyboard input reached Jarvis at all).")


if __name__ == "__main__":
    _key_test()
