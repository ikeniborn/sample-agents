"""Example collector for DSPy COPRO/MIPRO optimization (Variant 4).

Records (task_text, task_type, addendum, score) tuples from real benchmark
runs to data/dspy_examples.jsonl. When ≥ THRESHOLD real examples accumulate,
prints a hint to run optimize_prompts.py.

Synthetic cold-start examples live in data/dspy_synthetic.jsonl and are used
by optimize_prompts.py when fewer than THRESHOLD real examples are available.
"""
from __future__ import annotations

import json
from pathlib import Path

_EXAMPLES_PATH = Path(__file__).parent.parent / "data" / "dspy_examples.jsonl"
_SYNTHETIC_PATH = Path(__file__).parent.parent / "data" / "dspy_synthetic.jsonl"
_THRESHOLD = 30


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def record_example(
    task_text: str,
    task_type: str,
    addendum: str,
    score: float,
) -> None:
    """Append one (task, addendum, score) tuple to the JSONL example log.

    Creates logs/ directory if absent. Prints a hint to run the optimizer
    when the count first reaches THRESHOLD.
    """
    _EXAMPLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "task_text": task_text,
        "task_type": task_type,
        "addendum": addendum,
        "score": score,
    }
    with _EXAMPLES_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    count = _count_examples()
    if count == _THRESHOLD:
        print(
            f"[dspy] {_THRESHOLD} real examples collected "
            "→ run: uv run python optimize_prompts.py --target builder"
        )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load_examples(min_score: float = 0.8) -> list[dict]:
    """Return real examples from the JSONL log with score >= min_score."""
    if not _EXAMPLES_PATH.exists():
        return []
    examples: list[dict] = []
    with _EXAMPLES_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ex = json.loads(line)
                if ex.get("score", 0.0) >= min_score:
                    examples.append(ex)
            except json.JSONDecodeError:
                pass
    return examples


def load_synthetic() -> list[dict]:
    """Return cold-start synthetic examples from data/dspy_synthetic.jsonl."""
    if not _SYNTHETIC_PATH.exists():
        return []
    examples: list[dict] = []
    with _SYNTHETIC_PATH.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                examples.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return examples


def get_trainset(min_score: float = 0.8) -> list[dict]:
    """Return examples for optimization: real ones if ≥ THRESHOLD, else real + synthetic."""
    real = load_examples(min_score)
    if len(real) >= _THRESHOLD:
        return real
    synthetic = load_synthetic()
    # Real examples take precedence; synthetic fill the gap
    return real + synthetic


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _count_examples() -> int:
    if not _EXAMPLES_PATH.exists():
        return 0
    with _EXAMPLES_PATH.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())
