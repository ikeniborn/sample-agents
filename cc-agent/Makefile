.PHONY: sync run task

PAC1 := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))../pac1-py

sync:
	cd $(PAC1) && uv sync

run:
	cd $(PAC1) && uv run python ../cc-agent/runner.py

task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	cd $(PAC1) && uv run python ../cc-agent/runner.py $(TASKS)
