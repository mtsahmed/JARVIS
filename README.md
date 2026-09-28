# Jarvis

A voice-first personal assistant for one Windows laptop. Hold a key, ask a question, hear the answer.

Jarvis can answer questions, search the web, set reminders, search and read files in one allowed folder,
remember facts when asked, and give a daily morning briefing from Gmail (read-only). It starts at sign-in,
checks its own health, backs up its data daily, and warns before the $5/month budget runs out.

Requirements and roadmap: [`docs/requirements.md`](docs/requirements.md). Security review:
[`docs/threat-model.md`](docs/threat-model.md). Project rules: [`CLAUDE.md`](CLAUDE.md).

## Services

| Purpose | Service | Fallback |
|---|---|---|
| Language model | OpenAI (`gpt-5-mini`) | — |
| Speech to text | OpenAI `gpt-4o-mini-transcribe` | local faster-whisper `base.en` |
| Text to speech | Windows voice (SAPI) | — |
| Premium voice (briefing only) | ElevenLabs | Windows voice |
| Web search | Tavily | — |
| Email briefing | Gmail API, read-only | — |

Every service sits behind an adapter, and model names come from config.

## Setup (Windows, Python 3.11+)

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy env.example .env                 # settings; leave the key lines empty
.venv\Scripts\jarvis --secrets set    # API keys go into Windows Credential Manager
.venv\Scripts\jarvis --gmail-login    # optional: morning briefing from Gmail
.venv\Scripts\jarvis --autostart on   # optional: start at sign-in
.venv\Scripts\jarvis --health         # every check should say ok
```

## Use

| Command | What it does |
|---|---|
| `jarvis --voice` | Voice mode: hold **Right Ctrl**, speak, release |
| `jarvis` | Text chat |
| `jarvis --health` | Check every part of Jarvis (free) |
| `jarvis --usage` | This month's spending and free allowances |
| `jarvis --backup` / `--restore [YYYY-MM-DD]` | Back up now / list or restore backups |
| `jarvis --autostart on\|off\|status` | Start at Windows sign-in |
| `jarvis --secrets status\|set\|import` | Manage keys in Credential Manager |

Stop Jarvis with Ctrl+C or `/exit`. To start it again after closing the window, double-click
`scripts\run_jarvis.cmd`.

## Safety

- Every tool has a permission tier (AUTO, CONFIRM, BLOCKED); CONFIRM tools ask first.
- No tool can run shell commands, delete files, or send messages. Gmail is read-only.
- Every tool call is written to an audit log (secrets redacted).
- Web pages, emails and files are treated as untrusted text (prompt-injection guards and tests).
- Keys live in Windows Credential Manager; `.env`, `data/` and `logs/` are never committed.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

No network, microphone or keys needed.
