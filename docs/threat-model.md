# Jarvis — Threat model (Phase 6C)

Last reviewed: 2026-09-28 · Scope: Jarvis as built through Phase 6B (text + voice, reminders, file search/read,
web search, memory, Gmail read-only briefing). Owner: single user on a personal Windows laptop.

## 1. What is worth protecting

| Asset | Where it lives | Why it matters |
|---|---|---|
| API keys (OpenAI, ElevenLabs, Tavily) | Windows Credential Manager (primary); `.env` (owner-kept backup) | Money (pay-as-you-go), account abuse |
| Gmail read access (Google client + refresh token) | Credential Manager; `data/google_*.json` (owner-kept backup) | Read access to all email |
| Email content | Memory only while briefing; sent to OpenAI to summarize | Highly personal; attacker-controlled input |
| Files in the approved folder | `Documents\Files for JARVIS to Access`; extracted text in `data/files.db` | Personal documents |
| Rest of the disk | Everywhere else | Must stay out of reach |
| Remembered facts | `data/memory.db`; embeddings computed by OpenAI | Personal facts; persist across sessions |
| Reminders | `data/jarvis.db` | Personal schedule |
| Voice recordings | Memory only; sent to OpenAI for transcription (local fallback) | Your voice and what you say |
| Budget | Provider accounts | $5/month target |
| The laptop itself | — | Jarvis must never run code or change the system |

## 2. Ways data gets in (trust boundaries)

| Entry point | Trust | Notes |
|---|---|---|
| Your voice (push-to-talk) / typed text | Trusted | Only while the talk key is held; a nearby person or TV could speak during that time |
| Global keyboard hook (pynput) | — | Sees every key event system-wide; code compares only the talk key and never logs keys |
| Files in the approved folder | **Untrusted** | Could contain planted instructions |
| Web search results | **Untrusted** | Could contain planted instructions |
| Emails | **Untrusted** (attacker-controlled) | Anyone can email you |
| Remembered facts | Semi-trusted | Only saved when your own words ask; given to the model as data |
| LLM output (text and tool calls) | Untrusted | May be steered by any untrusted input above |
| Providers (OpenAI, Tavily, ElevenLabs, Google, Hugging Face) | External | Receive data; can fail |
| Python packages and the speech model download | External | Supply chain |

## 3. Threats, protections, and evidence

| # | Threat | Protection | Evidence (tests / checks) | Residual risk |
|---|---|---|---|---|
| T1 | API key leaked through Git, sharing, sync, or chat | Keys in Credential Manager (per user, this PC only, encrypted with your Windows sign-in); `.env`/`data/`/`logs/` gitignored; secret scan before every commit | `tests/test_secrets.py`; pre-commit scans (0 hits across all commits) | `.env` and Google files still hold plain-text backups (owner decision); remove with `jarvis --secrets import` |
| T2 | Keys leaked in logs or error messages | Log filter redacts known secret values; config hides keys from `repr`; provider error bodies are never shown or logged | `tests/test_config.py` (incl. `test_log_filter_redacts_secret_values`, 6C), `test_tts.py`, `test_web_search.py` | — |
| T3 | Model or tool reads `.env` / credentials | Jarvis project and `data/` excluded from file search even if inside the approved folder; hidden files skipped; only one approved folder | `tests/test_files.py`, `test_injection.py` | — |
| T4 | Prompt injection (file, web page, email) makes Jarvis act | Tiers: CONFIRM needs your spoken/typed yes; BLOCKED never runs; after outside text, web search and new reminders ask first (6B); `remember` only when your own words ask; outside text labelled untrusted | `tests/test_injection.py` (hijacked-model simulations), live checks with gpt-5-mini | A yes you give to a malicious request still counts: read back questions carefully |
| T5 | Destructive or outward actions | No tool deletes files, runs commands, or sends messages; Gmail scope is read-only (Google enforces) | Tool inventory in requirements §10–12; `gmail.SCOPES` test | — |
| T6 | Escaping the approved folder (`..\`, links, other drives) | Paths resolved (links followed) and must stay inside the folder; checked before asking you | `tests/test_files.py::test_refusal_rules` | — |
| T7 | Opening a file that runs code | Programs, scripts, shortcuts, installers, disk images, macro files, help/console files BLOCKED (list extended in 6C) | `tests/test_files.py` | A vulnerable viewer app (PDF, EPUB) could still be exploited by a malicious document: keep Windows and apps updated |
| T8 | Someone else speaks to Jarvis | Push-to-talk (key must be held); CONFIRM needs an explicit yes | `tests/test_push_to_talk.py`, voice confirm tests | A person next to you could say "yes"; lock the laptop when away |
| T9 | Keyboard hook abused as a keylogger | Only the talk key is compared; keys never stored or logged | Code review (`voice/push_to_talk.py`) | Any hook sees keys while running; it is Jarvis's own code only |
| T10 | Cost runaway | Output token cap; history trimmed; max 4 tool rounds; one search per question preferred; Tavily 1 credit/search; ElevenLabs quota check with fallback; per-request usage log | `tests/test_openai_adapter.py`, usage.jsonl | **No hard cap in Jarvis itself**: set spending limits in each provider dashboard (owner action) |
| T11 | Personal data sent to providers | Only what a task needs: request text + history (OpenAI), audio (OpenAI transcription, owner decision), memory text (OpenAI embeddings), email text only to summarize, file text only after your yes, search queries (Tavily) | Requirements §5 privacy decisions | Providers' own retention policies apply |
| T12 | Personal data at rest on the laptop | Everything local is in gitignored `data/` and `logs/`; forgotten memories are deleted; memory facts are **not** written to the audit log (fixed in 6C); logs rotate (1 MB × 3) | `tests/test_memory.py`, `test_registry.py` (6C) | `files.db` holds extracted text of approved files (same sensitivity as the files); reminder texts and search queries are in the audit log by design. Turn on Windows device encryption (owner action) |
| T13 | Malicious or vulnerable dependency / model | Version ranges in `pyproject.toml`; pure-Python choices where possible (pypdf, stdlib EPUB/HTTP); `pip-audit` scan (6D); speech model weights loaded by CTranslate2 (not executable pickle) | `pip-audit` 2026-09-28: 74 packages, no known vulnerabilities; exact known-good versions in `requirements-lock.txt` | Speech model download revision not pinned (weights only, loaded by CTranslate2) |
| T14 | Gmail token stolen | Refresh token in Credential Manager; read-only scope; expires every 7 days (Testing mode) | `tests/test_secrets.py` | Plain-text token file backup still present (owner decision) |
| T15 | Provider outage or network loss breaks Jarvis | Every provider call has a timeout and a clear error; local speech fallback; reminders work offline; text mode always works | Adapter tests; fallback tests | Offline voice turns wait for the cloud timeout (fixed in 6E) |
| T16 | Terminal escape sequences in web/email/file text printed to the console | Control characters stripped before printing (6C) | `tests/test_cli_output.py` (6C) | — |
| T17 | Reminder or briefing text visible on the lock screen via notifications | — | — | Accepted: turn off lock-screen notifications in Windows settings if this matters |

## 4. Owner actions (outside Jarvis's control)

1. Set a **monthly spending limit** in the OpenAI dashboard (and check ElevenLabs/Tavily plans stay free).
2. Turn on **Windows device encryption / BitLocker** (Settings > Privacy & security > Device encryption).
3. Keep **master copies of API keys** in a password manager (for `jarvis --secrets set` after a reinstall).
4. When ready, remove plain-text backups: `jarvis --secrets import` and answer **y**.
5. Keep Windows, Defender, and PDF/EPUB viewer apps updated; lock the laptop when away.

## 5. Accepted risks (summary)

A yes you give to a malicious request; a malicious document exploiting a viewer app; a person beside you saying
"yes"; provider-side data retention; no in-app spending cap (use provider limits); plain-text backups you chose to
keep; unpinned speech model revision; notification text on the lock screen.
