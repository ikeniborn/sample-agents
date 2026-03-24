from bitgn.vm.mini_connect import MiniRuntimeClientSync

from .loop import run_loop
from .prephase import run_prephase
from .prompt import system_prompt


def run_agent(model: str, harness_url: str, task_text: str, model_config: dict | None = None):
    """Universal agent entry point — works on any Obsidian vault without benchmark-specific logic."""
    vm = MiniRuntimeClientSync(harness_url)
    cfg = model_config or {}

    pre = run_prephase(vm, task_text, system_prompt)
    run_loop(vm, model, task_text, pre, cfg)
