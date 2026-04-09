.PHONY: run task

CC_AGENT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
PAC1     := $(CC_AGENT)../pac1-py
RUNNER   := $(CC_AGENT)runner.py

export PATH := $(HOME)/.local/bin:$(PATH)

run:
	cd $(PAC1) && uv run python $(RUNNER)

task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	cd $(PAC1) && uv run python $(RUNNER) $(TASKS)
