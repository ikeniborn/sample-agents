from __future__ import annotations

from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from .classifier import ModelRouter
from .loop import run_loop
from .prephase import run_prephase
from .prompt import system_prompt


def run_agent(router: ModelRouter, harness_url: str, task_text: str) -> dict:
    """Universal agent entry point for PAC1 benchmark using PCM runtime.
    Returns token usage stats dict: {input_tokens, output_tokens, thinking_tokens}."""
    vm = PcmRuntimeClientSync(harness_url)

    # FIX-117: prephase first — AGENTS.MD describes task complexity
    pre = run_prephase(vm, task_text, system_prompt)

    # Classify ONCE with full AGENTS.MD context (single LLM call)
    model, cfg, task_type = router.resolve_after_prephase(task_text, pre)

    stats = run_loop(vm, model, task_text, pre, cfg)
    stats["model_used"] = model
    stats["task_type"] = task_type
    return stats
