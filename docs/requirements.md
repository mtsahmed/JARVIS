# Jarvis — Requirements (v0.2)

Status: Phase 0 complete · Last updated: 2026-09-27
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
| Free disk | TBD | Needed before building the file index |

## 3. Core use cases (priority order)
**Primary interaction is voice** (spoken request → spoken reply). Text is the fallback and the development/debug interface. Every use case must be usable end-to-end by voice.

| # | Use case | Description | Access level |
|---|---|---|---|
| UC1 | Reminders | Create, list, and cancel reminders by voice; spoken alert plus desktop notification when due | Write (local only), auto-approved |
| UC2 | File search | Find files by name and content in **approved folders only**; read out top results, open the chosen file on request | Read-only |
| UC3 | Web search | Answer spoken questions with current web information; short spoken answer, sources shown on screen | Read-only |
| UC4 | Email briefing | Spoken daily summary of important unread Gmail messages | Read-only (no sending in v1) |

Later: Google Calendar, WhatsApp (official WhatsApp Business Platform only; unofficial personal-account automation violates WhatsApp's terms and risks bans), wake word, proactive suggestions.

## 4. Providers and budget
| Role | Provider (v1) | Cost basis | Monthly target |
|---|---|---|---|
| LLM (brain) | OpenAI API, cheapest model with reliable tool calling (exact model chosen in Phase 1 from current OpenAI docs) | Pay-as-you-go; $10 prepaid credit | ≤ $4 |
| Speech-to-text | Local CPU model first (candidate: faster-whisper, small model). Fallback: a cloud STT, chosen after benchmark. | Free if local | $0–1 |
| Text-to-speech (everyday replies) | Free built-in Windows voice (candidate: pyttsx3 over Windows SAPI5) | Free | $0 |
| Text-to-speech (premium) | ElevenLabs free tier (10,000 credits/month) used **only** for the daily email briefing; Flash model preferred | Free tier; personal use only (no commercial license) | $0 |
| Web search | Google Custom Search JSON API (existing key) | 100 queries/day free | $0 |
| **Total** | | | **≤ $5/month** |

**Search risk (must resolve before Phase 5 ends):** Google has closed the Custom Search JSON API to new customers and will **discontinue it on January 1, 2027**. New Programmable Search Engines are limited to specific sites (no whole-web search). Plan: (1) verify in Phase 3 that the existing key and engine can search the whole web; (2) keep search behind an adapter; (3) choose and test a replacement before December 2026.

**Voice upgrade path:** switching all replies to ElevenLabs is a config change. At voice-first usage (~225k characters/month) this needs a paid ElevenLabs plan (Creator tier or higher). Revisit after testing.

## 5. Non-functional requirements
- **Cost controls:** A spending/usage limit is set on every provider account. Output tokens are capped, conversation history is trimmed, and token usage and ElevenLabs characters are logged per request. Jarvis warns when monthly usage passes 80% of any limit.
- **Privacy:** Only the text needed for a task is sent to any provider. File contents are sent only when the user asks about them. Email bodies are summarized, never stored in full. Audio is processed locally when possible.
- **Security:** Secrets live in `.env` (never committed), later the Windows Credential Manager. Google OAuth uses read-only scopes. Every tool call is written to an audit log. A key that is ever exposed gets rotated immediately.
- **Control:** Permission tiers are AUTO (read-only / local reminders), CONFIRM (anything that sends, deletes, or modifies external data), and BLOCKED (shell commands, payments, credential access).
- **Voice:** Push-to-talk (hotkey) first; a wake word is optional later. Target is under 3 s from end of speech to start of reply (to be measured). Replies are short and speakable; details appear on screen.
- **Voice confirmations:** CONFIRM-tier actions are read back aloud and require a spoken "yes" (plus an on-screen button). Silence or unclear speech counts as "no".
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
| 1 | **Text core** ← current | Config, OpenAI adapter, CLI chat, logging, usage tracking | Multi-turn chat works; tokens logged; no secrets in Git |
| 2 | Voice | Push-to-talk, local STT/TTS, ElevenLabs adapter, STT benchmark | Spoken round trip works; latency and RAM measured |
| 3 | Tools + permissions | Reminders, file search, web search tools; permission tiers; audit log | All three tools work by voice; CONFIRM flow works by voice |
| 4 | Memory | Persistent facts and history, semantic recall | Recalls something from a previous session |
| 5 | Integrations & automation | Gmail read-only briefing, scheduled jobs, search replacement chosen | Morning briefing runs daily for a week |
| 6 | Hardening & tests | Test suite, prompt-injection tests, threat review | Tests pass; injection test blocked |
| 7 | Deployment & monitoring | Auto-start on login, health checks, backups, usage alerts | Survives reboot; alerts on failure or budget |
| 8 | Expansion | WhatsApp (official API), calendar, wake word, voice upgrade | Only after 1–7 are stable |

## 8. Open items
- Free disk space
- Which folders file search may index
- Confirm the Google Programmable Search Engine searches the whole web (not site-restricted)
- Replacement web search provider before Dec 2026
- Definition of "important" emails for the briefing
- STT benchmark: local model accuracy, latency, and RAM on this laptop
- Microphone/headset setup
