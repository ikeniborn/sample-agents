from __future__ import annotations

from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from .classifier import ModelRouter, reclassify_with_prephase
from .loop import run_loop
from .prephase import run_prephase
from .prompt import system_prompt


def run_agent(router: ModelRouter, harness_url: str, task_text: str) -> dict:
    """Universal agent entry point for PAC1 benchmark using PCM runtime.
    Returns token usage stats dict: {input_tokens, output_tokens, thinking_tokens}."""
    vm = PcmRuntimeClientSync(harness_url)

    model, cfg, task_type = router.resolve_llm(task_text)

    pre = run_prephase(vm, task_text, system_prompt)

    # FIX-89: refine task_type using vault context from prephase (AGENTS.MD + tree)
    refined = reclassify_with_prephase(task_type, task_text, pre)
    if refined != task_type:
        task_type = refined
        model, cfg = router.model_for_type(task_type)
        print(f"[MODEL_ROUTER][FIX-89] Reclassified → type={task_type}, model={model}")

    stats = run_loop(vm, model, task_text, pre, cfg)
    stats["model_used"] = model
    stats["task_type"] = task_type
    return stats
