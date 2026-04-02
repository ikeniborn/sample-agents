from __future__ import annotations

from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from .classifier import ModelRouter, TASK_CODER
from .loop import run_loop
from .prephase import run_prephase
from .prompt import system_prompt


def run_agent(router: ModelRouter, harness_url: str, task_text: str) -> dict:
    """Execute a single PAC1 benchmark task and return token usage statistics.

    Flow:
    1. run_prephase() — connects to the vault, fetches tree + AGENTS.MD + docs preload,
       builds the initial conversation log (system prompt, few-shot pair, vault context).
    2. router.resolve_after_prephase() — classifies the task type using AGENTS.MD as
       context (single LLM call or regex fast-path), then selects the appropriate model.
    3. run_loop() — executes up to 30 agent steps: LLM → tool dispatch → stall detection,
       compacting the log as needed. Ends when report_completion is called or steps run out.

    Returns a dict with keys: input_tokens, output_tokens, thinking_tokens, model_used,
    task_type.
    """
    vm = PcmRuntimeClientSync(harness_url)

    # Prephase first — AGENTS.MD describes task complexity and folder roles
    pre = run_prephase(vm, task_text, system_prompt)

    # Classify once with full AGENTS.MD context (single LLM call)
    model, cfg, task_type = router.resolve_after_prephase(task_text, pre)

    # FIX-163: compute coder sub-agent config (MODEL_CODER + coder ollama profile)
    coder_model = router.coder or model
    coder_cfg = router._adapt_config(router.configs.get(coder_model, {}), TASK_CODER)

    stats = run_loop(vm, model, task_text, pre, cfg, task_type=task_type,
                     coder_model=coder_model, coder_cfg=coder_cfg)
    stats["model_used"] = model
    stats["task_type"] = task_type
    return stats
