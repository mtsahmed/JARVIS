# CLAUDE.md — Jarvis project rules

## Project
Jarvis is a voice-first personal assistant (Python, Windows laptop). Full requirements and roadmap: `docs/requirements.md`.
**Current phase: 3 — Tools + permissions.** Do not build features from later phases.

## Providers (all behind adapters)
- LLM: OpenAI API → `src/jarvis/llm/`
- Speech-to-text: local model first → `src/jarvis/voice/stt/`
- Text-to-speech: local Windows voice, ElevenLabs for premium → `src/jarvis/voice/tts/`
- Web search: Google Custom Search JSON API (shuts down 2027-01-01, keep swappable) → `src/jarvis/tools/search/`
- Core code must never import a provider SDK directly; only through its adapter.
- Provider choice and model names come from config, never hardcoded.

## How to work
- Work in small steps. Propose a plan and wait for approval before creating more than 2–3 files.
- Never invent APIs, library functions, model names, or CLI flags. If unsure, say so and check the provider's official docs (OpenAI, ElevenLabs, Google).
- Ask before adding any dependency; record it in `pyproject.toml` with a reason.
- After each change, tell me how to verify it (a command to run and the expected output).

## Code standards
- Python 3.11+, type hints, `pathlib` for all paths (Windows).
- Layout: `src/jarvis/{core,llm,tools,memory,voice,integrations}`, tests in `tests/` (pytest).
- Use the `logging` module, not print, except for the CLI UI. Logs go to `logs/`.
- External calls have timeouts and clear error messages; a failing provider must not crash Jarvis.

## Security (non-negotiable)
- Secrets come only from `.env` via config. Never print, log, or commit them.
- `.env`, `.venv/`, `data/`, and `logs/` stay in `.gitignore`.
- Every tool declares a permission tier: AUTO, CONFIRM, or BLOCKED. CONFIRM tools must get explicit user approval at runtime.
- No tool may run arbitrary shell commands, delete files, or send messages in v1.
- Every tool call is written to the audit log: timestamp, tool, arguments (secrets redacted), result status.

## Cost (total budget $5/month)
- Use the cheapest adequate OpenAI model, cap output tokens, and trim history.
- Log usage per request: LLM tokens, ElevenLabs characters, search queries.
- ElevenLabs is used only where config allows (default: daily briefing); fall back to the local voice when quota is low.
