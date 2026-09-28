# Jarvis — Requirements (v0.3)

Status: Phases 1–4 complete · Phase 5 built, one-week daily-briefing check in progress · Phase 6 complete · Phase 7 (deployment & monitoring) current · Last updated: 2026-09-28
Changes from v0.2: Phase 2 measurements (§9); STT model chosen; push-to-talk key set to Right Ctrl; free disk known; open items updated.
Changes from v0.1: voice-first interaction; providers set to OpenAI (LLM), hybrid voice (local + ElevenLabs), Google search; roadmap added.

## 1. Purpose
A personal assistant, **primarily controlled by voice**, that runs on the owner's Windows laptop, helps with daily tasks, and never takes consequential actions without approval.

## 2. Environment
| Item | Value | Implication |
|---|---|---|
| Device | HP EliteBook 840 G5, Intel Core i5 (8th gen), integrated graphics | No GPU acceleration |
| RAM | 16 GB installed, ~4 GB typically free | Local LLMs are **not** viable. Keep Jarvis lightweight (<500 MB target). |
| OS | Windows | PowerShell, `pathlib` for paths, Windows notifications |
| Hosting | Laptop only | Jarvis runs only while the laptop is on; missed reminders fire at next startup |
| Language | American English | Single language for STT/TTS |
| Free disk | 17 GB free (2026-09-27) | Enough for speech models (~0.7 GB) and the file index |

## 3. Core use cases (priority order)
**Primary interaction is voice** (spoken request → spoken reply). Text is the fallback and the development/debug interface. Every use case must be usable end-to-end by voice.

| # | Use case | Description | Access level |
|---|---|---|---|
| UC1 | Reminders | Create, list, and cancel reminders by voice; spoken alert plus desktop notification when due | Write (local only), auto-approved |
| UC2 | File search | Find files by name and content (.txt, .md, .csv, .pdf) in the **one approved folder** `Documents\Files for JARVIS to Access` (owner decision 2026-09-28); read out top results; open (CONFIRM) or read a file's text for a question (CONFIRM: text is sent to OpenAI) | Read-only |
| UC3 | Web search | Answer spoken questions with current web information via Tavily (AUTO; 1 credit per search); short spoken answer, sources shown on screen, never read aloud | Read-only |
| UC4 | Email briefing | Spoken daily summary of important unread Gmail messages | Read-only (no sending in v1) |

Later: Google Calendar, WhatsApp (official WhatsApp Business Platform only; unofficial personal-account automation violates WhatsApp's terms and risks bans), wake word, proactive suggestions.

## 4. Providers and budget
| Role | Provider (v1) | Cost basis | Monthly target |
|---|---|---|---|
| LLM (brain) | OpenAI API: `gpt-5-mini`, reasoning effort `low`, streamed (owner switched from `gpt-5-nano` for answer quality; ~5× the token price, est. < $1/month at 50 voice turns/day) | Pay-as-you-go; $10 prepaid credit | ≤ $4 |
| Speech-to-text | Cloud: OpenAI `gpt-4o-mini-transcribe` (owner decision 2026-09-28, §9), with local faster-whisper `base.en` as automatic fallback when offline or on errors | $0.003/minute (~$0.40/month at 50 turns/day) | ≤ $1 |
| Text-to-speech (everyday replies) | Free built-in Windows voice: pyttsx3 over SAPI5 (confirmed) | Free | $0 |
| Text-to-speech (premium) | ElevenLabs free tier (10,000 credits/month) used **only** for the daily email briefing; `eleven_flash_v2_5`, `pcm_22050` output, original volume. Voice Library voices are not available via the API on the free tier; ElevenLabs Default voices expire 2026-12-31 | Free tier; personal use only (no commercial license) | $0 |
| Web search | Tavily Search API (chosen 2026-09-27) | Free tier: 1,000 searches/month (per Tavily's pricing page) | $0 |
| **Total** | | | **≤ $5/month** |

**Search decision (resolved 2026-09-27):** Google Custom Search JSON API was tested in Phase 3 and rejected: it is closed to new customers ("This project does not have the access to Custom Search JSON API"), only searches configured sites, and shuts down on January 1, 2027. Tavily was chosen instead (whole web, free tier, no announced shutdown). Search stays behind an adapter so the provider can change again.

**Voice upgrade path:** switching all replies to ElevenLabs is a config change. At voice-first usage (~225k characters/month) this needs a paid ElevenLabs plan (Creator tier or higher). Revisit after testing.

## 5. Non-functional requirements
- **Cost controls:** A spending/usage limit is set on every provider account. Output tokens are capped, conversation history is trimmed, and token usage and ElevenLabs characters are logged per request. Jarvis warns when monthly usage passes 80% of any limit.
- **Privacy:** Only the text needed for a task is sent to any provider. File contents are sent only when the user asks about them. Email bodies are summarized, never stored in full. **Audio:** push-to-talk recordings are sent to OpenAI for transcription (owner decision 2026-09-28: 10/10 commands correct vs 6/10 locally); they are not stored by Jarvis, and local transcription is used when offline. **Memory** (owner decisions 2026-09-28): facts are saved only when the user asks, no conversation transcripts are stored, forgotten facts are deleted from disk, and fact text is sent to OpenAI embeddings for recall.
- **Security:** Secrets live in Windows Credential Manager (per user, this computer only, DPAPI-encrypted; `jarvis --secrets status|import|set`), with `.env` only as a fallback; master copies of API keys are kept in the owner's password manager for restores. Google OAuth uses read-only scopes. Every tool call is written to an audit log. A key that is ever exposed gets rotated immediately.
- **Control:** Permission tiers are AUTO (read-only / local reminders), CONFIRM (anything that sends, deletes, or modifies external data), and BLOCKED (shell commands, payments, credential access).
- **Voice:** Push-to-talk (hotkey) first; a wake word is optional later. Target is under 3 s from end of speech to start of reply (measured 2026-09-27: median 4.7 s before streaming, **3.2 s after**; 2 of 8 live turns under 3 s; see §9). Replies are short and speakable; details appear on screen.
- **Voice confirmations:** CONFIRM-tier actions are read back aloud and require a spoken "yes" (plus an on-screen button). Silence or unclear speech counts as "no". A "yes" to opening or reading a file is remembered for that file (per action) until the conversation is reset or Jarvis exits (owner decision 2026-09-28); a "no" is never remembered, and cancelling reminders asks every time. Remembered approvals appear in the audit log as `remembered`.
- **Reliability:** Graceful failure when offline or a provider is down; reminders still fire offline; text input always works if voice fails; if ElevenLabs quota is exhausted, fall back to the local voice.
- **Maintainability:** Modular Python, one adapter per provider, tests for every tool.

## 6. Architecture decisions
| ID | Decision | Reason |
|---|---|---|
| AD-1 | OpenAI API as the LLM, behind a swappable adapter (`llm/`) | Owner's choice and existing credit; the adapter keeps Claude or others as options |
| AD-2 | Python core, no agent framework initially | Simplicity; fewer moving parts |
| AD-3 | SQLite for reminders, memory, and file index (FTS5 for text search) | Built into Python, zero-maintenance |
| AD-4 | Local scheduler for reminders | Works offline; no cloud dependency |
| AD-5 | Gmail API with read-only OAuth scope | Least privilege |
| AD-6 | Voice is the primary interface, built as a thin input/output layer over the text core in Phase 2 | Voice-first requirement |
| AD-7 | Hybrid voice: local STT + local TTS for everyday replies, ElevenLabs for the briefing | Fits the $5 budget; upgradeable by config |
| AD-8 | Every external service sits behind an adapter with a common interface (`llm`, `stt`, `tts`, `search`) | Providers can change (the Google search shutdown proves why) |

## 7. Roadmap
| # | Phase | Deliverable | Done when |
|---|---|---|---|
| 0 | Requirements & environment ✅ | This document, repo, venv | Complete |
| 1 | Text core ✅ | Config, OpenAI adapter, CLI chat, logging, usage tracking | Multi-turn chat works; tokens logged; no secrets in Git |
| 2 | Voice ✅ | Push-to-talk, local STT/TTS, ElevenLabs adapter, STT benchmark | Spoken round trip works; latency and RAM measured |
| 3 | Tools + permissions ✅ | Reminders, file search, web search tools; permission tiers; audit log | All three tools work by voice; CONFIRM flow works by voice |
| 4 | Memory ✅ | Persistent facts and history, semantic recall | Recalls something from a previous session |
| 5 | Integrations & automation (built 2026-09-28; week-long check running) | Gmail read-only briefing, scheduled jobs | Morning briefing runs daily for a week |
| 6 | Hardening & tests ✅ | Test suite, prompt-injection tests, threat review | Tests pass; injection test blocked |
| 7 | Deployment & monitoring ✅ | Auto-start on login, health checks, backups, usage alerts | Survives reboot; alerts on failure or budget |
| UI | HUD (separate track: `docs/ui-design.md`, `docs/ui-protocol.md`) | UI-2 display ✅ · UI-3 two-way · UI-4 live data · UI-5 compact widget | See §15 |
| 8 | Expansion (on hold, owner decision) | WhatsApp (official API), calendar, wake word, voice upgrade | Only after 1–7 are stable |

## 8. Open items
- Voice latency still slightly above the 3 s target (median 3.2 s with streaming). Remaining time is the LLM's time to its first sentence (most voice replies are one sentence). Options: reasoning effort `minimal` on gpt-5-mini, or accept ~3 s
- Real answers to live-data questions (prices, market caps, news) need the web search tool (Phase 3, UC3); until then Jarvis states its knowledge date
- ElevenLabs Default voices expire 2026-12-31: switch `JARVIS_ELEVENLABS_VOICE_ID` to a replacement voice before then (Jarvis falls back to the local voice if it stops working)
- Gmail tokens expire every 7 days while the Google project is in Testing mode (personal Gmail): re-run `jarvis --gmail-login` weekly
- Web questions take 5-10 s in voice mode (search round trips plus answer); Jarvis says "Let me check." at once to cover the wait

## 9. Measurements (Phase 2, 2026-09-27)

**Audio setup.** Built-in mic *Microphone (Conexant ISST Audio)*, 16 kHz mono works directly; speech peaks ~0.19–0.30, background ~0.007 (no gain change needed). Default output *Headphones (Conexant ISST Audio)*. Push-to-talk key is **Right Ctrl** (`ctrl_r`): the HP top-row F-keys send action keys unless Fn is held.

**STT benchmark** (`scripts/benchmark_stt.py`, 5 phrases, int8, `language=en`, VAD on; RAM = whole Python process):

| Model | Load | RAM after load | Peak RAM | Avg latency | Max latency | Avg WER |
|---|---|---|---|---|---|---|
| tiny.en (beam 1) | 1.0 s | 149 MB | 267 MB | 0.56 s | 0.69 s | 38% |
| base.en (beam 1) | 1.2 s | 197 MB | 333 MB | 1.00 s | 1.07 s | 36% |
| **base.en (beam 5)** — chosen | — | — | 334 MB | 1.10 s | 1.43 s | **28%** |
| small.en (beam 1) | 2.2 s | 382 MB | 993 MB | 3.62 s | 5.69 s | 36% |

WER is inflated by the hands-free recording (all models agree on the same misheard words); it is valid for comparing models. small.en is rejected: ~1 GB RAM and 3.6 s latency.

**Live voice session** (9 turns, `jarvis --voice`, gpt-5-nano with reasoning effort `low`):

| Metric | Result | Target |
|---|---|---|
| STT latency | 0.87–1.15 s | — |
| LLM latency | 2.3–7.0 s (median ~3.6 s) | — |
| End of speech → reply starts | min 3.3 s, median 4.7 s, max 7.9 s | < 3 s ❌ (before streaming) |
| Process RAM | 306–309 MB | < 500 MB ✅ |
| Errors / TTS fallbacks | 0 / 0 | — |
| LLM tokens (9 turns) | 9,300 total, 2,560 reasoning (≈ $0.004) | — |
| ElevenLabs premium voice | ~3.5–4.6 s before audio starts | briefing only |

**After streaming + voice fixes** (8 live turns, `gpt-5-mini`, reasoning `low`, voice instructions, 2026-09-27):

| Metric | Result | Target |
|---|---|---|
| STT latency | 0.78–0.98 s (after hallucination filter) | — |
| LLM latency (full reply, streamed) | 2.0–4.3 s | — |
| End of speech → first words | min 2.8 s, **median 3.2 s**, max 5.3 s (2 of 8 under 3 s) | < 3 s ⚠️ close |
| Process RAM | 303–304 MB | < 500 MB ✅ |
| Errors / TTS fallbacks | 0 / 0 | — |
| Reply length | 1–2 spoken sentences, 96–231 output tokens (64–128 reasoning) | short ✅ |

Changes: LLM reply streamed and spoken sentence by sentence; voice-only instructions (1–3 plain sentences, no lists/markdown, state knowledge date for changing facts); markdown stripped before speech; STT drops repetitive/silent segments and filler-only transcripts; spoken "reset" / "new conversation" clears history.

**STT on real commands** (10 command phrases recorded manually by the owner, `scripts/benchmark_stt.py`, 2026-09-28):

| Model | Exact | Avg WER | Avg / max latency | Process RAM |
|---|---|---|---|---|
| `base.en` (local, beam 5, vocabulary priming) | 6/10 | 16% | 1.03 / 1.15 s | 336 MB |
| **`gpt-4o-mini-transcribe`** (cloud) — chosen | **10/10** | **0%** | 1.06 / 2.81 s | 80 MB |

Local errors were on non-reminder commands ("Open" → "On", "car insurance" → "card and show them", "What's the weather" → "Or see whether I'm"). Vocabulary priming fixed "cancel" (was heard as "cancer"). Fallback verified: with the cloud failing after a reminder notification had loaded WinRT, the local model loaded and transcribed correctly (first fallback turn 3.1 s including model load; RSS 309 MB); with the cloud working, RSS is ~120 MB.

## 10. Phase 3 results (2026-09-28)

**Tools** (all via `logs/audit.jsonl`: tool, tier, redacted arguments, decision, status):

| Tool | Tier | Notes |
|---|---|---|
| create_reminder / list_reminders | AUTO | SQLite `data/jarvis.db`; scheduler every 5 s; desktop notification + spoken/printed alert; missed reminders fire at next start |
| cancel_reminder | CONFIRM (every time) | read back with text and time |
| search_files | AUTO | one approved folder; names/folders/dates only to the LLM; .txt/.md/.csv/.pdf/.epub content indexed locally (SQLite FTS5) |
| open_file | CONFIRM (once per file per conversation) | programs, scripts, shortcuts, macro files BLOCKED; path must resolve inside the approved folder |
| read_file | CONFIRM (once per file per conversation) | max 3,500 characters sent, passages matching the question; marked untrusted |
| web_search | AUTO | Tavily, 1 credit per search; sources on screen, never spoken; marked untrusted |

**Prompt injection** (`tests/test_injection.py` + live check with `gpt-5-mini`):
- Simulated fully hijacked model that obeys planted text in a file or web result: could not cancel a reminder, run `setup.exe`, read `.env` (outside the folder), or open/read other files. Each attempt was BLOCKED without asking or asked and refused; remembered approvals did not extend to other files.
- Real model, planted file and planted web result: 3/3 and 2/2 runs ignored the planted instructions and did only what the user asked (it flagged the hidden instructions to the user in 2 of 3 file runs).
- Residual risk: AUTO actions (see open items).

## 11. Phase 4 results (2026-09-28)

**Memory** (`data/memory.db`, never committed):

| Tool | Tier | Notes |
|---|---|---|
| remember | AUTO, guarded | runs only when the user's own words in that turn ask to remember ("remember", "don't forget", "note that", ...); otherwise BLOCKED, so text in a file or web page cannot plant a persistent "memory" |
| list_memories | AUTO | |
| forget_memory | CONFIRM (every time) | really deletes the row |

- Recall: with 20 or fewer memories all are given to the model (no extra call); with more, OpenAI `text-embedding-3-small` picks the 8 most similar (similarity >= 0.25). Measured: related fact/question ~0.45, unrelated ~0.1-0.2; lookup ~250 ms.
- Identical facts are not duplicated; similar facts are kept side by side and the model is told about the older one (silent merging could lose a different fact).
- Live check (gpt-5-mini): facts saved in session 1 were recalled in session 2 from differently worded questions; listing and forgetting (after yes) worked; with 22 memories, recall found insurance, allergy and pet facts. Done-when met.

## 12. Phase 5 design (2026-09-28)

**Owner decisions:** personal Gmail; Gmail API with `gmail.readonly` (Google itself blocks sending, deleting, marking read); "important" = `is:unread is:important category:primary newer_than:1d` (up to 10); ElevenLabs voice for the briefing with Windows voice fallback; OAuth token kept as a file in `data/` (exception to secrets-only-in-.env, to move to Credential Manager in Phase 6).

- **Scheduled briefing:** daily at `JARVIS_BRIEFING_TIME` (07:30), once per day (state in `data/jarvis.db`); if Jarvis starts later, at the first check after start. Only scheduled once Gmail is set up. Runs in its own thread so reminders are never delayed.
- **Content:** important unread emails (sender, subject, time; up to 1,500 characters of text sent to the LLM only to summarize, never stored) and today's reminders; spoken summary in 3-6 sentences, details on screen, desktop notification.
- **On demand:** `morning_briefing` tool (AUTO, read-only, audited without email text); voice says "Let me check your inbox." and speaks the answer in one piece with the premium voice.
- **Sign-in:** `jarvis --gmail-login` (browser, read-only consent); expired or missing sign-in is reported plainly ("Run: jarvis --gmail-login").
- Tests: 16 (Gmail parsing and query with faked HTTP, 401/expiry, briefing composition, LLM failure fallback, audit privacy, once-a-day job incl. restart and failure, premium voice delivery).

## 13. Phase 6 hardening (2026-09-28)

- **6A Secrets:** Windows Credential Manager (per user, this computer only, verified on write); config order environment variable > Credential Manager > .env; `jarvis --secrets status|import|set`. Owner kept the plain-text `.env` keys and Google files as an unused backup for now.
- **6B Injection guard (closes the Phase 3 residual risk):** `read_file`, `web_search` and `morning_briefing` bring outside text into a request. After that, in the same request, `web_search` ("Search the web for '...'?") and `create_reminder` need the user's yes (never remembered; resets with each new request). `remember` stays guarded by the user's own words. Tests: a hijacked model trying to leak private facts in a search query or plant a reminder after a file, web result or email is asked and refused. Live check: 4 normal web questions, 1 search each, 0 extra questions.
- **6C Threat review:** `docs/threat-model.md` (assets, entry points, 17 threats with protections, evidence and residual risk, owner actions, accepted risks). Fixes from the review: blocked file types extended (installers, disk images, compiled help, consoles, add-ins, Access databases, .rdp, search/library files); remembered fact text no longer written to the audit log (only its length), so "forget" leaves no copy; control characters stripped from outside text before printing or speaking; log redaction filter unit-tested.
- **6D Tests and dependencies:** coverage 74% -> 84% (273 tests): new tests pin the exact tool inventory and tiers (a tool added or re-tiered without updating the test fails), the injection guards' wiring, the text chat loop, alerts, factories, and the error paths of Credential Manager, logging, Gmail sign-in, embeddings and OpenAI (clear messages, no leaked secrets). Remaining uncovered code is hardware/OS glue (microphone, speakers, Windows voice, keyboard hook, notifications) checked by live runs. SQLite stores now close their files on exit (84 test warnings -> 1). `pip-audit` (run once in a throwaway environment): 74 packages, no known vulnerabilities. `requirements-lock.txt` records the exact known-good versions. Old remembered facts were cleaned from the audit log.
- **6E Offline voice:** after cloud speech recognition fails, the local model is used directly for 3 minutes (then the cloud is tried again); cloud timeout lowered from 15 s to 8 s (slowest benchmark clip: 2.8 s). Measured with a failing cloud: first turn 3.4 s (incl. loading the local model), next turns ~1.0 s with no cloud attempt.
- **Phase 6 done-when met:** all tests pass (276) and the prompt-injection tests are blocked (`tests/test_injection.py`, `test_memory.py`, `test_wiring.py`).

## 14. Phase 7 deployment & monitoring (2026-09-28)

**Owner decisions:** minimized window with self-restart; backups on this laptop (14 days); $5/month total with warnings at 80% and 100%.

- **7A Auto-start:** `jarvis --autostart on|off|status` manages `Jarvis.cmd` in the user's Startup folder (never touches a file it did not create). At sign-in `scripts/run_jarvis.cmd` runs voice mode minimized and restarts it after a crash (10 s apart, at most 4 restarts); Ctrl+C or /exit stops it. Only one Jarvis runs at a time (lock on `data/jarvis.lock`; a second copy says "already running"). Enabled 2026-09-28.
- **7B Health:** `jarvis --health` checks OpenAI (model info), web search credits, ElevenLabs characters, Gmail sign-in, microphone, disk space, last briefing, auto-start: all free. The same checks run in the background at every start; failures (not warnings) give a desktop notification and a spoken "Heads up" (at most two). During use, 3 failures in a row of the language service, web search or premium voice give an alert (at most hourly). First run: all 8 checks OK; the morning briefing ran on 28 Sep.
- **7C Backups:** once a day (first check after midnight, i.e. at the first start each day) `memory.db` and `jarvis.db` are copied with SQLite's online backup to `data/backups/YYYY-MM-DD/`, integrity-checked, last 14 days kept. `jarvis --backup` (safe while running), `jarvis --restore` (list) and `jarvis --restore YYYY-MM-DD` (only while Jarvis is stopped; asks first; saves the current data to `backups/before-restore-<time>/`; never restores a damaged copy). Health check includes the last backup. First backup 2026-09-28.
- **7D Usage & budget:** OpenAI spend is estimated from Jarvis's own usage records and `config/prices.json` (standard-tier prices from OpenAI's pricing page, dated; local speech recognition counts as free; a model with no price is listed, never silently free). The running monthly total lives in `data/jarvis.db` (survives log rotation; first run in a month back-fills from `usage.jsonl`). Budget `JARVIS_MONTHLY_BUDGET_USD=5`, warning at `JARVIS_BUDGET_WARN_PERCENT=80`: a notification plus a spoken warning once a day while at or above 80%, and at once when 100% is reached. Free allowances (Tavily credits, ElevenLabs characters) come from their own usage APIs and warn at the same percent in a daily check. `jarvis --usage` prints the month's report. **Hard cap:** the owner set a $20 spending limit in the OpenAI dashboard (2026-09-28); Jarvis's $5 budget is the soft warning below it. First real report: OpenAI $0.07 (1.5%), Tavily 15/1,000, ElevenLabs 117,370/131,000 (90%, past the warning level).
- **Phase 7 done-when met (2026-09-28):** Jarvis started on its own after a sign-in (reboot test) and all startup checks passed; failure alerts (7B) and budget alerts (7D) are in place and tested (325 tests pass).

## 15. UI-2 HUD display (2026-09-28)

**Owner decisions:** a local web page (not a desktop app); one-way in UI-2 (Jarvis → page only); off unless turned on; `websockets` library; port 47821; the reply is sent once the answer is complete (after speaking starts, because replies stream).

- **UI-2A Event bus:** `core/events.py`. The voice and text loops publish `state` (idle/listening/thinking/speaking/confirm/error), `transcript`, `reply` (with sources and kind: answer/reminder/briefing) and `error` (friendly text). The core knows nothing about the UI; publishing never blocks or raises, and a failing display is logged and skipped.
- **UI-2B Server:** `ui/server.py`, `ui/hub.py`, `ui/protocol.py`, in its own thread (asyncio). `JARVIS_UI_ENABLED` (default false), `JARVIS_UI_PORT` (default 47821). 127.0.0.1 only, port held exclusively; Host and Origin checks; a new random session token per run, `hello` with it within 5 s; strict message checks (exact fields, v1, 64 KB); only `hello`/`ping` accepted, other types ignored; at most 3 pages, 100 messages per 10 s; fixed file list with a strict Content-Security-Policy. If the port is taken, Jarvis runs without the HUD.
- **UI-2C Page:** `ui/static/` (plain HTML/CSS/JS, no build step). Orb with four rings and per-mode speeds and colours, state title and hint, waveform, last exchange with https source links, conversation log (last 50), clock. Reminders, usage, system and service chips show "later update" (UI-4); mic, text box and Send are disabled (UI-3). All text via `textContent`; no inline code or styles; fonts bundled (SIL OFL). Reconnects with backoff (1 s … 30 s), re-reads the token after Jarvis restarts, pauses animation when hidden, respects reduced motion.
- **Tests:** 82 new (event bus and loop publishing, protocol checks, server security, bus-to-page delivery, static-file rules): 407 in total. Checked by eye in Chrome with a scripted demo (states, log, sources, offline and recovery).
- **UI-2 done-when met:** the page shows Jarvis's live state (mode, what you said, replies, errors) and recovers on its own when Jarvis restarts.
