from bitgn.vm.pcm_connect import PcmRuntimeClientSync

from .loop import run_loop
from .prephase import run_prephase
from .prompt import system_prompt



def run_agent(model: str, harness_url: str, task_text: str, model_config: dict | None = None) -> dict:
    """Universal agent entry point for PAC1 benchmark using PCM runtime.
    Returns token usage stats dict: {input_tokens, output_tokens, thinking_tokens}."""
    vm = PcmRuntimeClientSync(harness_url)
    cfg = model_config or {}

    pre = run_prephase(vm, task_text, system_prompt)
    return run_loop(vm, model, task_text, pre, cfg)
