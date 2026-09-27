"""STT benchmark: accuracy, latency, and RAM of faster-whisper models on this laptop.

Usage (from the project root):
    .venv\\Scripts\\python scripts\\benchmark_stt.py record    # read the command phrases aloud (once)
    .venv\\Scripts\\python scripts\\benchmark_stt.py record --timed   # hands-free: hear, beep, repeat
    .venv\\Scripts\\python scripts\\benchmark_stt.py run       # benchmark tiny.en, base.en, small.en
    .venv\\Scripts\\python scripts\\benchmark_stt.py run --models base.en distil-small.en

Clips and results go to data/benchmark/ (gitignored). Each model is measured in a fresh
subprocess so RAM numbers are not polluted by previously loaded models.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import psutil

from jarvis.core.config import load_voice_config

# Real Jarvis commands, including words that were misheard in use ("cancel" -> "cancer").
PROMPTS = (
    "Remind me to stretch in two minutes.",
    "Cancel all my reminders.",
    "What reminders do I have for tomorrow?",
    "Remind me to call the dentist tomorrow at nine.",
    "Open my budget spreadsheet.",
    "Find the file about my car insurance.",
    "What's the weather going to be like this weekend?",
    "Search the web for the latest news about Apple.",
    "What is the market cap of Apple?",
    "Cancel the reminder about the dentist.",
)
# "openai:<model>" measures OpenAI's cloud transcription (sends the clips to OpenAI; ~$0.003/minute).
DEFAULT_MODELS = ("base.en",)
SAMPLE_RATE = 16_000
RESULT_MARKER = "BENCHMARK_RESULT "


def bench_dir() -> Path:
    return load_voice_config().data_dir / "benchmark" / "commands"


def clip_path(i: int) -> Path:
    return bench_dir() / f"clip{i + 1}.wav"


# --- recording -------------------------------------------------------------------------------


def write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        data = f.readframes(f.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def beep(freq: float = 880.0, seconds: float = 0.2) -> None:
    from jarvis.voice.audio import play

    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    play((0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32), SAMPLE_RATE)


def say(text: str) -> None:
    # Benchmark tooling only; Jarvis itself speaks through the TTS adapter.
    import pyttsx3

    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()


def record_timed(seconds: float) -> None:
    """Hands-free: reads each phrase aloud, beeps, then records the speaker repeating it."""
    from jarvis.voice.audio import Recorder

    bench_dir().mkdir(parents=True, exist_ok=True)
    say("Benchmark. After each beep, repeat the phrase you just heard.")
    for i, prompt in enumerate(PROMPTS):
        say(f"Phrase {i + 1}. {prompt}")
        beep()
        recorder = Recorder(max_seconds=seconds)
        recorder.start()
        time.sleep(seconds)
        samples = recorder.stop()
        beep(440.0)
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        write_wav(clip_path(i), samples)
        warn = "  (very quiet - consider re-recording)" if peak < 0.05 else ""
        print(f'[{i + 1}/{len(PROMPTS)}] "{prompt}": saved {samples.size / SAMPLE_RATE:.1f}s, peak {peak:.2f}{warn}', flush=True)
    say("Done. Thank you.")


def record() -> None:
    from jarvis.voice.audio import Recorder

    bench_dir().mkdir(parents=True, exist_ok=True)
    print(f"You will read {len(PROMPTS)} short phrases. For each: press Enter, read it, press Enter again.\n")
    for i, prompt in enumerate(PROMPTS):
        print(f'[{i + 1}/{len(PROMPTS)}]  "{prompt}"')
        input("   Enter to START...")
        recorder = Recorder(max_seconds=15)
        recorder.start()
        input("   Recording... Enter to STOP.")
        samples = recorder.stop()
        peak = float(np.abs(samples).max()) if samples.size else 0.0
        write_wav(clip_path(i), samples)
        warn = "  (very quiet - consider re-recording)" if peak < 0.05 else ""
        print(f"   saved {samples.size / SAMPLE_RATE:.1f}s, peak {peak:.2f}{warn}\n")
    print(f"Clips saved in {bench_dir()}. Now run:  .venv\\Scripts\\python scripts\\benchmark_stt.py run")


# --- measurement (runs in a subprocess per model) ------------------------------------------


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1_048_576


def worker(model: str, download_only: bool) -> None:
    from jarvis.voice.stt.factory import create_stt

    config = load_voice_config()
    provider, api_key = None, None
    if model.startswith("openai:"):
        from jarvis.core.config import load_config

        provider, model, api_key = "openai", model.split(":", 1)[1], load_config().llm_api_key
    if download_only:
        if provider is None:  # cloud models have nothing to download
            create_stt(config, model=model)
        return

    clips = [read_wav(clip_path(i)) for i in range(len(PROMPTS))]
    peak = [rss_mb()]
    stop = threading.Event()

    def sample_peak() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], rss_mb())
            time.sleep(0.02)

    sampler = threading.Thread(target=sample_peak, daemon=True)
    sampler.start()
    rss_before = rss_mb()
    started = time.perf_counter()
    stt = create_stt(config, model=model, provider=provider, api_key=api_key)
    load_s = time.perf_counter() - started
    rss_loaded = rss_mb()

    results = []
    for samples in clips:
        started = time.perf_counter()
        transcript = stt.transcribe(samples)
        results.append({"text": transcript.text, "audio_s": transcript.audio_seconds,
                        "latency_s": time.perf_counter() - started})
    stop.set()
    sampler.join()
    print(RESULT_MARKER + json.dumps({
        "model": model, "load_s": load_s, "rss_before_mb": rss_before, "rss_loaded_mb": rss_loaded,
        "peak_rss_mb": max(peak[0], rss_mb()), "clips": results,
        "beam_size": config.stt_beam_size, "compute_type": config.stt_compute_type,
    }))


# --- scoring and report ----------------------------------------------------------------------


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: word-level edit distance / reference length."""
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / max(len(ref), 1)


def run_subprocess(model: str, download_only: bool) -> dict | None:
    args = [sys.executable, __file__, "_worker", model] + (["--download-only"] if download_only else [])
    proc = subprocess.run(args, capture_output=not download_only, text=True)
    if proc.returncode != 0:
        print(f"   {model}: failed (exit {proc.returncode})")
        if proc.stderr:
            print(proc.stderr[-1500:])
        return None
    if download_only:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER):])
    return None


def run(models: list[str]) -> None:
    missing = [clip_path(i).name for i in range(len(PROMPTS)) if not clip_path(i).is_file()]
    if missing:
        sys.exit(f"Missing clips {missing}. Record them first:  .venv\\Scripts\\python scripts\\benchmark_stt.py record")

    rows = []
    for model in models:
        print(f"== {model}: making sure the model is downloaded...")
        run_subprocess(model, download_only=True)
        print(f"== {model}: measuring...")
        result = run_subprocess(model, download_only=False)
        if result:
            rows.append(result)

    lines = [
        f"# STT benchmark — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Machine: {platform.processor()} · {psutil.cpu_count(logical=False)} cores / "
        f"{psutil.cpu_count()} threads · {psutil.virtual_memory().total / 2**30:.1f} GB RAM "
        f"({psutil.virtual_memory().available / 2**30:.1f} GB free at run time)",
        "",
        "| Model | Load s | RAM after load MB | Peak RAM MB | Avg latency s | Max latency s | Real-time factor | Avg WER |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        clips = r["clips"]
        latencies = [c["latency_s"] for c in clips]
        rtf = sum(latencies) / max(sum(c["audio_s"] for c in clips), 1e-9)
        avg_wer = sum(wer(p, c["text"]) for p, c in zip(PROMPTS, clips)) / len(clips)
        lines.append(
            f"| {r['model']} | {r['load_s']:.1f} | {r['rss_loaded_mb']:.0f} | {r['peak_rss_mb']:.0f} | "
            f"{sum(latencies) / len(latencies):.2f} | {max(latencies):.2f} | {rtf:.2f} | {avg_wer:.0%} |"
        )
    if rows:
        lines += ["", f"Settings: compute_type={rows[0]['compute_type']}, beam_size={rows[0]['beam_size']}, "
                  "language=en, vad_filter=True. RAM = whole Python process (RSS).", "", "## Transcripts", ""]
        for r in rows:
            lines.append(f"**{r['model']}**")
            for p, c in zip(PROMPTS, r["clips"]):
                lines.append(f"- ({wer(p, c['text']):.0%}) {c['text'] or '(nothing)'}")
            lines.append("")

    report = "\n".join(lines)
    out = bench_dir() / "results.md"
    out.write_text(report, encoding="utf-8")
    print("\n" + report + f"\nSaved to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    record_parser = sub.add_parser("record", help="record the 5 benchmark phrases")
    record_parser.add_argument("--timed", action="store_true", help="hands-free: hear each phrase, beep, repeat it")
    record_parser.add_argument("--seconds", type=float, default=6.0, help="seconds per phrase in --timed mode")
    run_parser = sub.add_parser("run", help="benchmark models on the recorded clips")
    run_parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    worker_parser = sub.add_parser("_worker")  # internal
    worker_parser.add_argument("model")
    worker_parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()

    if args.command == "record":
        if args.timed:
            record_timed(args.seconds)
        else:
            record()
    elif args.command == "run":
        run(args.models)
    else:
        worker(args.model, args.download_only)


if __name__ == "__main__":
    main()
