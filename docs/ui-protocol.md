# Jarvis UI Protocol (v1)

Status: Draft for UI-2 · Last updated: 2026-09-28
Scope: messages between the Jarvis core (Python) and the HUD front end over a local WebSocket.

## 1. Principles
- **The server is the source of truth.** The UI only displays state and relays user input. It never calls tools, changes permissions, or decides anything.
- **Local only.** Server binds to `127.0.0.1`. No remote access.
- **Every message is untrusted on arrival.** Both sides validate; unknown types are ignored and logged.
- **All text is rendered as text** (`textContent`), never as HTML. Replies can contain web content from search results.

## 2. Connection
- Page: `http://127.0.0.1:<port>/` · WebSocket: `ws://127.0.0.1:<port>/ws`
- `<port>` comes from config: `JARVIS_UI_PORT`, default `47821`.
- On startup the server generates a random session token and injects it into the served page.
- The client's **first message must be `hello`** with that token. Wrong token, or no `hello` within 5 s → server closes the connection.
- Server also rejects connections whose `Origin` header is not the local page.
- Heartbeat: client sends `ping` every 15 s; server answers `pong`. No `pong` for 30 s → client reconnects with backoff (1 s, 2 s, 4 s … max 30 s).
- After every (re)connect, the server sends a full `snapshot`.

## 3. Envelope (every message)
```json
{ "v": 1, "type": "state", "id": "uuid4", "ts": "2026-09-28T09:41:05Z", "data": { } }
```
| Field | Rule |
|---|---|
| `v` | Protocol version. Mismatch → server sends `error` and closes. |
| `type` | One of the types below |
| `id` | Unique per message (uuid4) |
| `ts` | UTC, ISO 8601 |
| `data` | Type-specific payload (may be `{}`) |

Max message size: 64 KB. Max `text_input` length: 2,000 characters.

## 4. Server → UI
| Type | `data` | When |
|---|---|---|
| `hello_ack` | `{ "session": "id", "server_version": "x.y.z" }` | After a valid `hello` |
| `snapshot` | `{ "mode", "last_user", "last_reply", "log": [...], "reminders": [...], "usage": [...], "providers": [...], "system": {...} }` (same shapes as below) | On connect/reconnect |
| `state` | `{ "mode": "idle" \| "listening" \| "thinking" \| "speaking" \| "confirm" \| "error", "detail": "optional text" }` | Every mode change |
| `transcript` | `{ "text": "...", "final": false }` | Speech-to-text partial/final results (today's speech recognition is batch, so only `final: true` is sent; text typed in the terminal is sent the same way) |
| `reply` | `{ "text": "...", "sources": [ { "title": "...", "url": "https://..." } ], "kind": "answer" \| "reminder" \| "briefing" }` | Jarvis's answer (sources optional; `kind` defaults to answer). In voice mode it is sent once the answer is complete, which is **after** `state speaking`, because Jarvis starts speaking while the reply is still streaming |
| `audio_level` | `{ "source": "mic" \| "tts", "level": 0.0–1.0 }` | Max 20/s, only while listening or speaking |
| `confirm_request` | `{ "request_id": "uuid4", "action": "Send WhatsApp message", "target": "...", "summary": "...", "tier": "CONFIRM", "expires_in_s": 15 }` | A CONFIRM-tier tool needs approval |
| `confirm_result` | `{ "request_id": "...", "outcome": "approved" \| "denied" \| "expired", "by": "voice" \| "screen" \| "timeout" }` | Decision made (from any channel) |
| `reminders` | `{ "items": [ { "id": "...", "due_at": "ISO 8601", "text": "..." } ] }` | On change |
| `usage` | `{ "items": [ { "provider": "openai", "used": 1.62, "limit": 4.0, "unit": "USD" \| "credits" \| "queries", "period": "month" \| "day" } ] }` | On change, max once per 10 s |
| `providers` | `{ "items": [ { "name": "llm" \| "stt" \| "tts" \| "search", "status": "ok" \| "degraded" \| "down", "detail": "optional" } ] }` | On change |
| `system` | `{ "cpu_pct": 18, "mem_used_gb": 9.8, "mem_total_gb": 16, "mic": "Headset · ready", "voice_engine": "local" \| "elevenlabs" }` | Every 5 s |
| `error` | `{ "code": "provider_down", "message": "user-friendly text", "recoverable": true }` | Any user-visible failure |
| `pong` | `{}` | Reply to `ping` |

## 5. UI → Server
| Type | `data` | Server behavior |
|---|---|---|
| `hello` | `{ "token": "...", "client": "hud" \| "widget" }` | Must be first; validates token |
| `ptt` | `{ "action": "start" \| "stop" \| "cancel" }` | Same effect as the push-to-talk hotkey |
| `text_input` | `{ "text": "..." }` | Treated exactly like a spoken request |
| `confirm_response` | `{ "request_id": "...", "approved": true }` | Passed to the permission layer (see §6) |
| `ping` | `{}` | Server replies `pong` |

There are intentionally **no other commands**. The UI cannot run tools, change settings, or read files.

## 6. Confirmation rules
1. Only the permission layer creates a `confirm_request`. The UI shows it; the voice layer asks aloud at the same time.
2. The **first** answer wins (voice or screen). Every later answer for that `request_id` is ignored and logged.
3. `request_id` is single-use and must match a pending request, otherwise ignore and log.
4. No answer before `expires_in_s` → **denied** (`outcome: "expired"`).
5. The server always sends `confirm_result`; the UI closes the dialog only on that message, never on its own click.
6. Every decision goes to the audit log with `by`.

## 7. Example sequences
**Voice turn:** `ptt start` → `state listening` → `audio_level`… → `transcript (final:false)`… → `ptt stop` → `state thinking` → `transcript (final:true)` → `state speaking` → `audio_level`… → `reply` (once the whole answer is known) → `state idle`

**Typed turn:** `text_input` → `state thinking` → `reply` → `state speaking` → `state idle`

**Confirmation:** `state confirm` + `confirm_request` → user says "yes" or clicks Approve → `confirm_result approved` → `state speaking` ("Done.") → `state idle`

## 8. Phase scope
| Phase | Message types |
|---|---|
| UI-2 (one-way) | Server: `hello_ack`, `snapshot`, `state`, `transcript`, `reply`, `error`, `pong`. Client: `hello`, `ping` only. |
| UI-3 (two-way) | + `ptt`, `text_input`, `confirm_request`, `confirm_response`, `confirm_result` |
| UI-4 (live data) | + `audio_level`, `reminders`, `usage`, `providers`, `system` |
