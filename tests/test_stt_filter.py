from types import SimpleNamespace

from jarvis.voice.stt.faster_whisper_adapter import _looks_hallucinated


def segment(compression_ratio: float = 1.2, no_speech_prob: float = 0.1, avg_logprob: float = -0.3) -> SimpleNamespace:
    return SimpleNamespace(compression_ratio=compression_ratio, no_speech_prob=no_speech_prob, avg_logprob=avg_logprob)


def test_normal_speech_kept() -> None:
    assert not _looks_hallucinated(segment())


def test_repetitive_segment_dropped() -> None:
    assert _looks_hallucinated(segment(compression_ratio=3.1))


def test_likely_silence_dropped() -> None:
    assert _looks_hallucinated(segment(no_speech_prob=0.9, avg_logprob=-1.4))


def test_confident_speech_with_some_noise_kept() -> None:
    # high no-speech probability alone is not enough (same rule as faster-whisper)
    assert not _looks_hallucinated(segment(no_speech_prob=0.9, avg_logprob=-0.4))
