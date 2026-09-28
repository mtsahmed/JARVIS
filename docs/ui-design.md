# Jarvis HUD — Design Spec (v1)

Approved design canvas: https://claude.ai/artifact/Ud3hDRW624mNCnn3GXpuKz
Reference size 1440×900; layout must adapt to smaller laptop screens (1366×768 minimum).

## Tokens
| Token | Value | Use |
|---|---|---|
| `--bg` | `#070B12` | Page background, with a 40 px grid of 1 px lines at `rgba(91,214,240,0.035)` |
| `--panel` | `rgba(10,20,32,0.7)` | Panels, 10 px radius |
| `--border` | `rgba(91,214,240,0.2)` | Panel and divider lines |
| `--text` | `#E6F4F8` | Primary text |
| `--text-2` | `#8FA9B8` | Labels, secondary text |
| `--accent` | `#5BD6F0` | Jarvis, active states, rings |
| `--amber` | `#F2A541` | User labels, confirmation, degraded status |
| Display font | Rajdhani 600/700, uppercase, letter-spacing 0.22–0.32em | Headings, state title, buttons |
| Body font | IBM Plex Mono 400/500, 12–14 px | Everything else |

Fonts are open-source (SIL OFL). Bundle them locally in `static/fonts/` so the HUD works offline; fall back to `Segoe UI` / `Consolas`.

## Layout
- **Header (56 px):** wordmark · status pill (dot + mode) · provider chips (LLM, STT, TTS, SEARCH) · clock and date.
- **Body, three columns:** left 320 px (Reminders, Usage) · center flexible (orb, state title and hint, waveform, last exchange) · right 340 px (Conversation log, System).
- **Footer (72 px):** mic button (60 px circle) · text input · Send button.
- **Confirmation dialog:** full-screen dim overlay, 540 px card with amber border; Action / To / Message / Tier rows; "Say yes to approve" hint; Deny (outline) and Approve (amber) buttons.
- The "Demo state" buttons in the design are for the mockup only. Do not ship them.

## Orb
Four concentric SVG rings plus a glowing core (sizes at 420 px orb):
| Layer | Diameter | Stroke | Dash | Rotation |
|---|---|---|---|---|
| Ring 1 | 408 | 1.5 | `2 9`, 50% opacity | clockwise |
| Ring 2 | 344 | 2 | `140 36 18 36`, 75% | counter-clockwise |
| Ring 3 | 272 | 1 | `4 6`, 55% | clockwise |
| Ring 4 | 208 | 7, round caps | `70 93`, 90% | counter-clockwise |
| Core | 140 | 2 px border, radial fill, glow | — | pulse (scale 1 → 1.07) |

## States
| Mode | Title | Ring periods (1/2/3/4) | Core pulse | Waveform | Color |
|---|---|---|---|---|---|
| idle | STANDING BY | 70s / 48s / 90s / 30s | 6s | flat | accent |
| listening | LISTENING | 30s / 20s / 40s / 12s | 1.6s | mic level | accent, pill dot blinks |
| thinking | THINKING | 8s / 5s / 12s / 2.2s | 1.1s | flat | accent |
| speaking | SPEAKING | 24s / 16s / 30s / 9s | 0.8s | TTS level | accent |
| confirm | AWAITING APPROVAL | 50s / 36s / 60s / 20s | 2s | flat | amber, dialog open |
| error | ERROR | stopped | none | flat | amber + message from `error` |

Waveform: 32 bars, 4 px wide, 4 px gap, height scaled by `audio_level` (in UI-2/3, before live levels exist, use a gentle idle animation while listening/speaking).

## Behavior
- Respect `prefers-reduced-motion`: stop rotations and pulses.
- Pause animations when the page is hidden (`visibilitychange`) to save CPU.
- Keyboard: every control reachable with Tab; visible focus ring; icon buttons have `aria-label`.
- Conversation log keeps the last 50 entries in the DOM.

## Compact widget (UI-5)
380×540 window: wordmark + expand button, 220 px orb, state title, last reply, mic button.
