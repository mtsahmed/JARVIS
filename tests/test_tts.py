import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

import jarvis.voice.tts.elevenlabs_adapter as el
from jarvis.voice.tts.base import TTSError
from jarvis.voice.tts.factory import Speaker


class FakeTTS:
    def __init__(self, name: str, fail: bool = False, chars_left: int | None = None) -> None:
        self._name = name
        self._fail = fail
        self._chars_left = chars_left
        self.spoken: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def speak(self, text: str) -> None:
        if self._fail:
            raise TTSError(f"{self._name} down")
        self.spoken.append(text)

    def characters_left(self) -> int:
        if self._chars_left is None:
            raise TTSError("quota unknown")
        return self._chars_left


def make(premium: FakeTTS | None = None, min_chars: int = 100) -> tuple[Speaker, FakeTTS, list[dict[str, Any]]]:
    local, records = FakeTTS("sapi"), []
    return Speaker(local, premium, min_chars_left=min_chars, usage_sink=records.append), local, records


# --- Speaker: routing, fallback, usage ---------------------------------------------------------


def test_local_only() -> None:
    speaker, local, records = make()
    assert speaker.speak("hello") == "sapi"
    assert local.spoken == ["hello"]
    assert records[0]["provider"] == "sapi" and records[0]["fallback"] is False


def test_premium_used_when_quota_ok() -> None:
    premium = FakeTTS("elevenlabs", chars_left=5000)
    speaker, local, records = make(premium)
    assert speaker.speak("hello") == "elevenlabs"
    assert premium.spoken == ["hello"] and local.spoken == []
    assert records[0]["characters"] == 5


def test_premium_error_falls_back_to_local() -> None:
    speaker, local, records = make(FakeTTS("elevenlabs", fail=True, chars_left=5000))
    assert speaker.speak("hello") == "sapi"
    assert local.spoken == ["hello"]
    assert records[0]["fallback"] is True


def test_low_quota_uses_local() -> None:
    premium = FakeTTS("elevenlabs", chars_left=102)
    speaker, local, records = make(premium, min_chars=100)
    assert speaker.speak("hello") == "sapi"  # 102 - 5 < 100
    assert premium.spoken == []
    assert records[0]["fallback"] is True


def test_unknown_quota_still_tries_premium() -> None:
    premium = FakeTTS("elevenlabs", chars_left=None)
    speaker, _, _ = make(premium)
    assert speaker.speak("hello") == "elevenlabs"


def test_empty_text_not_spoken_or_logged() -> None:
    speaker, local, records = make()
    assert speaker.speak("   ") == "none"
    assert local.spoken == [] and records == []


def test_usage_record_has_no_text() -> None:
    speaker, _, records = make()
    speaker.speak("my private reminder")
    assert "my private reminder" not in json.dumps(records)


# --- ElevenLabs adapter over HTTP (urlopen faked) ------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.fixture
def adapter() -> el.ElevenLabsAdapter:
    return el.ElevenLabsAdapter("el-secret-key", "voice/123", "eleven_flash_v2_5", timeout_seconds=5)


def test_speak_posts_request_and_plays_pcm(monkeypatch: pytest.MonkeyPatch, adapter: el.ElevenLabsAdapter) -> None:
    sent: list[urllib.request.Request] = []
    played: list[tuple[bytes, int]] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: sent.append(req) or FakeResponse(b"\x01\x00\x02\x00"))
    monkeypatch.setattr(el, "play_pcm16", lambda data, rate: played.append((data, rate)))

    adapter.speak("Hi")

    req = sent[0]
    assert req.get_method() == "POST"
    assert req.full_url == f"{el.API_BASE}/text-to-speech/voice%2F123?output_format=pcm_22050"
    assert req.get_header("Xi-api-key") == "el-secret-key"
    assert json.loads(req.data) == {"text": "Hi", "model_id": "eleven_flash_v2_5"}
    assert played == [(b"\x01\x00\x02\x00", 22_050)]


def test_characters_left(monkeypatch: pytest.MonkeyPatch, adapter: el.ElevenLabsAdapter) -> None:
    body = json.dumps({"character_count": 1200, "character_limit": 10000}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse(body))
    assert adapter.characters_left() == 8800


def test_http_401_gives_clear_error_without_secrets(monkeypatch: pytest.MonkeyPatch, adapter: el.ElevenLabsAdapter) -> None:
    def fail(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, io.BytesIO(b'{"detail": "key el-secret-key"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(TTSError, match="401") as exc:
        adapter.speak("Hi")
    assert "el-secret-key" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_network_error_gives_clear_error(monkeypatch: pytest.MonkeyPatch, adapter: el.ElevenLabsAdapter) -> None:
    def fail(req: urllib.request.Request, timeout: float) -> FakeResponse:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(TTSError, match="Could not reach ElevenLabs"):
        adapter.characters_left()
