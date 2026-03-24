# AICODE-NOTE: Keep these wrappers aligned with the README commands so the sample
# stays trivial to run from a fresh checkout without inventing parallel workflows.

.PHONY: sync run task run-universal task-universal

sync:
	uv sync

run:
	uv run python main.py

task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	uv run python main.py $(TASKS)

run-universal:
	uv run python main_universal.py

task-universal:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task-universal TASKS='t01 t03'"; exit 1; fi
	uv run python main_universal.py $(TASKS)
