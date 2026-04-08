from __future__ import annotations

import os

from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from .classifier import ModelRouter, TASK_CODER
from .loop import run_loop
from .prephase import run_prephase
from .prompt import build_system_prompt
from .prompt_builder import build_dynamic_addendum

_PROMPT_BUILDER_ENABLED = os.getenv("PROMPT_BUILDER_ENABLED", "0") == "1"
_PROMPT_BUILDER_MAX_TOKENS = int(os.getenv("PROMPT_BUILDER_MAX_TOKENS", "300"))


def run_agent(router: ModelRouter, harness_url: str, task_text: str) -> dict:
    """Execute a single PAC1 benchmark task and return token usage statistics.

    Flow:
    1. run_prephase() — connects to the vault, fetches tree + AGENTS.MD + docs preload,
       builds the initial conversation log (placeholder system prompt, few-shot pair,
       vault context).
    2. router.resolve_after_prephase() — classifies the task type using AGENTS.MD as
       context (single LLM call or regex fast-path), then selects the appropriate model.
    3. build_system_prompt(task_type) — assembles a task-type specific system prompt
       from modular blocks (FIX-NNN step 1: always).
    4. build_dynamic_addendum() — if PROMPT_BUILDER_ENABLED=1 and task type is in
       _NEEDS_BUILDER, calls a lightweight LLM to generate task-specific guidance
       (FIX-NNN step 2: optional, for ambiguous types only).
    5. run_loop() — executes up to 30 agent steps: LLM → tool dispatch → stall detection,
       compacting the log as needed. Ends when report_completion is called or steps run out.

    Returns a dict with keys: input_tokens, output_tokens, thinking_tokens, model_used,
    task_type.
    """
    vm = PcmRuntimeClientSync(harness_url)

    # Prephase with empty placeholder — system prompt is assembled below after classification
    pre = run_prephase(vm, task_text, "")

    # Classify once with full AGENTS.MD context (single LLM call)
    model, cfg, task_type = router.resolve_after_prephase(task_text, pre)

    # FIX-NNN step 1: modular prompt assembly — always, zero extra latency
    base_prompt = build_system_prompt(task_type)

    # FIX-NNN step 2: LLM-based addendum — only for ambiguous/complex task types
    addendum = ""
    builder_in_tok = builder_out_tok = 0
    if _PROMPT_BUILDER_ENABLED:
        builder_model = router.prompt_builder or router.classifier or model
        builder_cfg = router._adapt_config(
            router.configs.get(builder_model, {}), "classifier"  # low temperature
        )
        addendum, builder_in_tok, builder_out_tok = build_dynamic_addendum(
            task_text=task_text,
            task_type=task_type,
            agents_md=pre.agents_md_content,
            vault_tree=pre.vault_tree_text,
            model=builder_model,
            cfg=builder_cfg,
            max_tokens=_PROMPT_BUILDER_MAX_TOKENS,
        )

    final_prompt = base_prompt
    if addendum:
        final_prompt += "\n\n## TASK-SPECIFIC GUIDANCE\n" + addendum

    # Inject assembled prompt into log[0] and sync preserve_prefix
    pre.log[0]["content"] = final_prompt
    pre.preserve_prefix[0]["content"] = final_prompt

    # FIX-163: compute coder sub-agent config (MODEL_CODER + coder ollama profile)
    coder_model = router.coder or model
    coder_cfg = router._adapt_config(router.configs.get(coder_model, {}), TASK_CODER)

    # FIX-218: evaluator sub-agent config
    evaluator_model = router.evaluator or model
    evaluator_cfg = router._adapt_config(router.configs.get(evaluator_model, {}), "evaluator")

    stats = run_loop(vm, model, task_text, pre, cfg, task_type=task_type,
                     coder_model=coder_model, coder_cfg=coder_cfg,
                     evaluator_model=evaluator_model, evaluator_cfg=evaluator_cfg)
    stats["model_used"] = model
    stats["task_type"] = task_type
    stats["builder_used"] = bool(addendum)
    stats["builder_in_tok"] = builder_in_tok
    stats["builder_out_tok"] = builder_out_tok
    return stats
