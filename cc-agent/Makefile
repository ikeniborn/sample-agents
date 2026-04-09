.PHONY: run task task-sonnet task-opus run-single task-single

CC_AGENT := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
PAC1     := $(CC_AGENT)../pac1-py
RUNNER   := $(CC_AGENT)runner.py

export PATH := $(HOME)/.local/bin:$(PATH)

# Multi-agent (default)
run:
	cd $(PAC1) && uv run python $(RUNNER)

task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	cd $(PAC1) && uv run python $(RUNNER) $(TASKS)

SELF := $(abspath $(lastword $(MAKEFILE_LIST)))

# Legacy single-agent
run-single:
	MULTI_AGENT=0 $(MAKE) -f $(SELF) run

task-single:
	MULTI_AGENT=0 $(MAKE) -f $(SELF) task

# Model shortcuts
task-sonnet:
	CLAUDE_MODEL=sonnet $(MAKE) -f $(SELF) task

task-opus:
	CLAUDE_MODEL=opus $(MAKE) -f $(SELF) task
