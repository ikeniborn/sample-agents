"""DSPy COPRO optimizer for prompt_builder and evaluator (Variant 4).

Loads real examples from logs/dspy_examples.jsonl (accumulated by main.py runs).
If fewer than 30 real examples are available, supplements with cold-start synthetic
examples from data/dspy_synthetic.jsonl.

Saves compiled programs to:
  data/prompt_builder_program.json  — loaded by agent/prompt_builder.py at startup
  data/evaluator_program.json       — loaded by agent/evaluator.py at startup

Usage:
    uv run python optimize_prompts.py --target builder
    uv run python optimize_prompts.py --target evaluator
    uv run python optimize_prompts.py --target all

Environment requirements (same as main.py — loaded from .env / .secrets):
    MODEL_DEFAULT or MODEL_CLASSIFIER — used as optimizer LM
    ANTHROPIC_API_KEY / OPENROUTER_API_KEY / OLLAMA_BASE_URL
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env / .secrets before any agent imports
# ---------------------------------------------------------------------------

def _load_file(path: "str | Path") -> None:
    try:
        for line in Path(path).read_text().splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, _, v = s.partition("=")
                k = k.strip()
                v = v.partition("#")[0].strip()
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


_BASE = Path(__file__).parent
_load_file(_BASE / ".env")
_load_file(_BASE / ".secrets")

import dspy
from dspy.teleprompt import COPRO

from agent.dspy_lm import DispatchLM
from agent.dspy_examples import get_trainset
from agent.prompt_builder import PromptAddendum
from agent.evaluator import EvaluateCompletion

_BUILDER_PROGRAM_PATH = _BASE / "data" / "prompt_builder_program.json"
_EVAL_PROGRAM_PATH = _BASE / "data" / "evaluator_program.json"


# ---------------------------------------------------------------------------
# Model setup
# ---------------------------------------------------------------------------

def _get_optimizer_model() -> tuple[str, dict]:
    """Return (model_id, cfg) for the optimizer LM.

    Prefers MODEL_CLASSIFIER (fast, low-temperature) → MODEL_DEFAULT fallback.
    Loads full config from models.json.
    """
    import json

    models_path = _BASE / "models.json"
    with models_path.open() as fh:
        models_raw: dict = json.load(fh)

    profiles: dict = models_raw.get("_profiles", {})

    def _resolve(cfg: dict) -> dict:
        resolved = {}
        for k, v in cfg.items():
            if isinstance(v, str) and v in profiles:
                resolved[k] = profiles[v]
            else:
                resolved[k] = v
        return resolved

    all_cfgs = {k: _resolve(v) for k, v in models_raw.items() if not k.startswith("_")}

    model = (
        os.environ.get("MODEL_CLASSIFIER")
        or os.environ.get("MODEL_DEFAULT")
        or next(iter(all_cfgs), None)
    )
    if not model:
        raise RuntimeError("No model configured. Set MODEL_DEFAULT in .env")
    cfg = all_cfgs.get(model, {})
    return model, cfg


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _builder_metric(example: dspy.Example, prediction, _trace=None) -> float:
    """Score an addendum prediction: 1.0 if source example score >= 0.8, else 0.0.

    During COPRO optimisation, examples with score < 0.8 already signal
    poor quality — the metric propagates that signal to the optimizer.
    """
    source_score: float = getattr(example, "score", 1.0)
    if source_score < 0.8:
        return 0.0
    # Penalise empty or very short addendum (< 3 bullet points)
    addendum: str = getattr(prediction, "addendum", "") or ""
    bullet_count = sum(1 for line in addendum.splitlines() if line.strip().startswith("-"))
    if bullet_count < 2:
        return 0.5
    return 1.0


def _evaluator_metric(example: dspy.Example, prediction, _trace=None) -> float:
    """Score evaluator prediction against expected approved_str label."""
    expected: str = getattr(example, "approved_str", "yes")
    predicted: str = (getattr(prediction, "approved_str", "") or "").strip().lower()
    return 1.0 if predicted == expected.lower() else 0.0


# ---------------------------------------------------------------------------
# Training set builders
# ---------------------------------------------------------------------------

def _builder_trainset(min_score: float = 0.7) -> list[dspy.Example]:
    """Build DSPy Examples for prompt_builder optimisation."""
    raw = get_trainset(min_score=min_score)
    examples = []
    for ex in raw:
        examples.append(
            dspy.Example(
                task_type=ex.get("task_type", "default"),
                task_text=ex.get("task_text", ""),
                vault_tree="",   # not available in collected examples; builder adapts
                agents_md="",
                addendum=ex.get("addendum", ""),
                score=ex.get("score", 1.0),
            ).with_inputs("task_type", "task_text", "vault_tree", "agents_md")
        )
    return examples


def _evaluator_trainset() -> list[dspy.Example]:
    """Build DSPy Examples for evaluator optimisation from synthetic data.

    A minimal evaluator dataset: approved + rejected cases derived from
    synthetic examples. Real evaluator labelling requires manual annotation
    and is outside the scope of automatic collection.
    """
    # Approved examples (agent completed task correctly)
    approved_cases = [
        dspy.Example(
            task_text="Send an email to John Smith about the project update",
            task_type="email",
            proposed_outcome="OUTCOME_OK",
            agent_message="Email sent to john@example.com",
            done_ops="- WRITTEN: /outbox/5.json",
            completed_steps="- wrote outbox/5.json",
            skepticism_level="mid",
            approved_str="yes",
            issues_str="",
            correction_hint="",
        ).with_inputs("task_text", "task_type", "proposed_outcome", "agent_message",
                      "done_ops", "completed_steps", "skepticism_level"),
        dspy.Example(
            task_text="What is the email of Maria Schulz?",
            task_type="lookup",
            proposed_outcome="OUTCOME_OK",
            agent_message="maria@acme.com",
            done_ops="(none)",
            completed_steps="- read contacts/cont_007.json",
            skepticism_level="mid",
            approved_str="yes",
            issues_str="",
            correction_hint="",
        ).with_inputs("task_text", "task_type", "proposed_outcome", "agent_message",
                      "done_ops", "completed_steps", "skepticism_level"),
    ]
    # Rejected examples (agent outcome incorrect)
    rejected_cases = [
        dspy.Example(
            task_text="Delete all processed items from the archive folder",
            task_type="longContext",
            proposed_outcome="OUTCOME_OK",
            agent_message="Done",
            done_ops="(none)",
            completed_steps="- listed archive/",
            skepticism_level="mid",
            approved_str="no",
            issues_str="OUTCOME_OK but task required file deletions and done_ops is empty",
            correction_hint="OUTCOME_NONE_CLARIFICATION",
        ).with_inputs("task_text", "task_type", "proposed_outcome", "agent_message",
                      "done_ops", "completed_steps", "skepticism_level"),
        dspy.Example(
            task_text="Pr...",
            task_type="inbox",
            proposed_outcome="OUTCOME_OK",
            agent_message="processed",
            done_ops="(none)",
            completed_steps="",
            skepticism_level="mid",
            approved_str="no",
            issues_str="Task text is truncated/garbled — should be CLARIFICATION",
            correction_hint="OUTCOME_NONE_CLARIFICATION",
        ).with_inputs("task_text", "task_type", "proposed_outcome", "agent_message",
                      "done_ops", "completed_steps", "skepticism_level"),
    ]
    return approved_cases + rejected_cases


# ---------------------------------------------------------------------------
# Optimisation runners
# ---------------------------------------------------------------------------

def optimize_builder(model: str, cfg: dict, min_score: float = 0.8) -> None:
    """Run COPRO on the PromptAddendum Signature and save compiled program."""
    trainset = _builder_trainset(min_score=min_score)
    if not trainset:
        print("[optimize] No training examples found. Run main.py first to collect examples.")
        sys.exit(1)

    print(f"[optimize] Builder trainset: {len(trainset)} examples")
    print(f"[optimize] Model: {model}")

    lm = DispatchLM(model, cfg, max_tokens=400)
    dspy.configure(lm=lm)

    program = dspy.Predict(PromptAddendum)
    teleprompter = COPRO(metric=_builder_metric, breadth=4, depth=2, init_temperature=0.9)

    compiled = teleprompter.compile(
        program,
        trainset=trainset,
        eval_kwargs={"num_threads": 1, "display_progress": True, "display_table": 0},
    )

    _BUILDER_PROGRAM_PATH.parent.mkdir(exist_ok=True)
    compiled.save(str(_BUILDER_PROGRAM_PATH))
    print(f"[optimize] Builder program saved → {_BUILDER_PROGRAM_PATH}")


def optimize_evaluator(model: str, cfg: dict) -> None:
    """Run COPRO on the EvaluateCompletion Signature and save compiled program."""
    trainset = _evaluator_trainset()
    print(f"[optimize] Evaluator trainset: {len(trainset)} examples")
    print(f"[optimize] Model: {model}")

    lm = DispatchLM(model, cfg, max_tokens=600)
    dspy.configure(lm=lm)

    program = dspy.ChainOfThought(EvaluateCompletion)
    teleprompter = COPRO(metric=_evaluator_metric, breadth=4, depth=2, init_temperature=0.9)

    compiled = teleprompter.compile(
        program,
        trainset=trainset,
        eval_kwargs={"num_threads": 1, "display_progress": True, "display_table": 0},
    )

    _EVAL_PROGRAM_PATH.parent.mkdir(exist_ok=True)
    compiled.save(str(_EVAL_PROGRAM_PATH))
    print(f"[optimize] Evaluator program saved → {_EVAL_PROGRAM_PATH}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DSPy COPRO optimizer for pac1-py prompt_builder and evaluator."
    )
    parser.add_argument(
        "--target",
        choices=["builder", "evaluator", "all"],
        default="all",
        help="Which program to optimise (default: all)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.8,
        help="Minimum task score to include real examples (default: 0.8)",
    )
    args = parser.parse_args()

    model, cfg = _get_optimizer_model()

    if args.target in ("builder", "all"):
        optimize_builder(model, cfg, min_score=args.min_score)

    if args.target in ("evaluator", "all"):
        optimize_evaluator(model, cfg)

    print("[optimize] Done.")


if __name__ == "__main__":
    main()
