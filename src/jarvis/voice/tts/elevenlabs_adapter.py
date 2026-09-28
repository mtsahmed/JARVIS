"""ElevenLabs premium voice over its REST API (stdlib HTTP only, no SDK).

Endpoints (ElevenLabs API reference):
- POST /v1/text-to-speech/{voice_id}?output_format=pcm_22050  body {text, model_id} -> raw PCM
- GET  /v1/user/subscription  -> character_count, character_limit
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from jarvis.voice.audio import AudioError, play_pcm16
from jarvis.voice.tts.base import TTSError

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1"
# 44.1 kHz PCM needs the Pro tier; 22.05 kHz works on the free tier.
OUTPUT_FORMAT = "pcm_22050"
SAMPLE_RATE = 22_050

_STATUS_MESSAGES = {
    401: "ElevenLabs rejected the API key or its permissions (401). Check ELEVENLABS_API_KEY.",
    402: "ElevenLabs quota or plan limit reached (402).",
    404: "ElevenLabs voice not found (404). Check JARVIS_ELEVENLABS_VOICE_ID.",
    422: "ElevenLabs rejected the request (422). Check the voice ID and model.",
    429: "ElevenLabs rate limit or quota reached (429).",
}


class ElevenLabsAdapter:
    def __init__(self, api_key: str, voice_id: str, model: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return "elevenlabs"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> bytes:
        request = urllib.request.Request(
            API_BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"xi-api-key": self._api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        # Error bodies/headers are not passed on; "from None" keeps them out of tracebacks.
        except urllib.error.HTTPError as e:
            e.close()
            logger.warning("ElevenLabs %s %s -> HTTP %s", method, path.split("?")[0], e.code)
            raise TTSError(_STATUS_MESSAGES.get(e.code, f"ElevenLabs returned an error (HTTP {e.code}).")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("ElevenLabs connection problem: %s", type(e).__name__)
            raise TTSError("Could not reach ElevenLabs. Check your internet connection.") from None

    def quota(self) -> tuple[int, int]:
        """(characters used, character limit) in the current billing period. Raises TTSError."""
        try:
            data = json.loads(self._request("GET", "/user/subscription"))
            return int(data["character_count"]), int(data["character_limit"])
        except (ValueError, KeyError, TypeError):
            raise TTSError("Unexpected quota response from ElevenLabs.") from None

    def characters_left(self) -> int:
        """Characters remaining in the current billing period. Raises TTSError."""
        used, limit = self.quota()
        return limit - used

    def speak(self, text: str) -> None:
        path = f"/text-to-speech/{urllib.parse.quote(self._voice_id, safe='')}?output_format={OUTPUT_FORMAT}"
        pcm = self._request("POST", path, {"text": text, "model_id": self._model})
        if len(pcm) < 2:
            raise TTSError("ElevenLabs returned no audio.")
        try:
            play_pcm16(pcm, SAMPLE_RATE)  # original volume, by owner decision
        except AudioError as e:
            raise TTSError(str(e)) from None
