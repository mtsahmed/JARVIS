import pytest
from pynput import keyboard

from jarvis.voice.push_to_talk import PushToTalk, parse_key


def test_press_release_cycle() -> None:
    ptt = PushToTalk()
    assert ptt.press() == "start"
    assert ptt.release() == "stop"


def test_key_auto_repeat_ignored() -> None:
    ptt = PushToTalk()
    events = [ptt.press(), ptt.press(), ptt.press(), ptt.release()]
    assert events == ["start", None, None, "stop"]


def test_release_without_press_ignored() -> None:
    assert PushToTalk().release() is None


def test_parse_named_and_char_keys() -> None:
    assert parse_key("ctrl_r") == keyboard.Key.ctrl_r
    assert parse_key(" F9 ") == keyboard.Key.f9
    assert parse_key("j") == keyboard.KeyCode.from_char("j")


def test_parse_unknown_key_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown push-to-talk key"):
        parse_key("f99")
