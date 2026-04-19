# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Constraints

- Target directory: `pac1-coder/` only
- Do NOT modify `.secrets`
- Do NOT hardcode — work through logic when extending agent behavior
- Never edit pac1-coder/.env and pac1-coder/.secrets
- Start agent only from dev git branch

## Commands

```bash
# Install dependencies
make sync                             # or: uv sync

# Run all tasks
uv run python main.py                 # or: make run

# Run specific tasks
uv run python main.py t01 t03
```

## Architecture

### Entry points

- `main.py` — benchmark runner: connects to `api.bitgn.com`, iterates tasks, prints summary table

### Agent execution flow (`agent/`)

```
main.py → run_agent() [__init__.py]
  ├── ModelRouter.resolve() [classifier.py]  ← classify task type, pick model
  ├── run_prephase() [prephase.py]           ← tree + read AGENTS.MD → PrephaseResult
  └── run_loop() [loop.py]                   ← 30-step loop, returns token stats
        ├── compact log (keep prefix + last 5 pairs) [log_compaction.py]
        ├── call LLM → NextStep [dispatch.py]
        ├── stall detection [stall.py, FIX-74]
        ├── security gates [security.py, FIX-203/206/214/215/250]
        ├── JSON extraction [json_extract.py, FIX-146]
        └── dispatch tool → PCM runtime
```

### Extracted modules (decomposed from loop.py)

| Module | Contents |
|--------|----------|
| `agent/log_compaction.py` | `_StepFact`, `build_digest`, `_compact_log`, `_compact_tool_result`, `_history_action_repr` |
| `agent/json_extract.py` | 7-level JSON extraction (`_extract_json_from_text`), `_normalize_parsed` |
| `agent/stall.py` | `_check_stall`, `_handle_stall_retry` (dependency-injected `call_llm_fn`) |
| `agent/security.py` | FIX-203/206/214/215/250 constants and functions, `_check_write_scope` |
| `agent/tracer.py` | JSONL replay tracer — `init_tracer`, `get_task_tracer`, `TaskTracer.emit` |

### Replay tracer (`agent/tracer.py`)

Controlled by `TRACE_ENABLED=1` env var (default: 0, no overhead in prod).
Output: `logs/{ts}_{model}/traces.jsonl` — one JSON line per event.

Event types: `task_start`, `llm_response`, `dispatch_result`, `stall_detected`, `evaluator_call`, `task_end`.

```bash
TRACE_ENABLED=1 uv run python main.py t01
cat logs/*/traces.jsonl | python -c "import sys,json; [print(json.loads(l)['event']) for l in sys.stdin]"
```

### LLM dispatch (`agent/dispatch.py`)

Three-tier fallback: **Anthropic SDK → OpenRouter → Ollama**

- Anthropic: Pydantic structured output, native thinking blocks
- OpenRouter: probes `json_schema` → `json_object` → text fallback
- Ollama: `json_object` mode, optional `{"think": true}` via `extra_body`

Capability detection cached per model via `_STATIC_HINTS` and runtime probes.

### Task type classifier (`agent/classifier.py`)

Routes to different models per task type via env vars:

| Type | Keywords | Env var |
|------|----------|---------|
| THINK | distill, analyze, compare | `MODEL_THINK` |
| TOOL | delete, move, rename | `MODEL_TOOL` |
| LONG_CONTEXT | 3+ paths, "all files" | `MODEL_LONG_CONTEXT` |
| DEFAULT | everything else | `MODEL_DEFAULT` |

### Evaluator/Critic (`agent/evaluator.py`)

Pre-completion review: separate LLM checks agent outcome vs evidence before `report_completion`.
Configured via `EVALUATOR_ENABLED`, `EVAL_SKEPTICISM`, `EVAL_EFFICIENCY`, `EVAL_MAX_REJECTIONS`.
Fail-open on errors. Skips if <30s remaining. See `docs/evaluator.md`.

### Stall detection (`loop.py`, FIX-74)

Three signals, all task-agnostic:
1. Same tool+args fingerprint 3× in a row → inject hint
2. Same path error ≥2× → inject hint with path + error code
3. ≥6 steps without write/delete/move/mkdir → inject hint

Resets on any successful write/delete/move/mkdir.

### Prompt strategy (`agent/prompt.py`, `agent/prompt_builder.py`)

**Dynamic system prompt** (FIX-NNN): assembled from task-type specific blocks.
`build_system_prompt(task_type)` selects relevant sections only:
- `email` → core + email workflow + delete workflow
- `inbox` → core + inbox workflow + delete workflow
- `lookup` / `think` / `distill` → core only
- `longContext` → core + delete workflow
- `default` → full prompt (all blocks, safe fallback)

LLM addendum via `prompt_builder.py` (enabled by default, `PROMPT_BUILDER_ENABLED=0` to disable):
activated for all task types.

**Discovery-first**: zero hardcoded vault paths. Agent discovers folder roles from:
1. Pre-loaded AGENTS.MD (from prephase)
2. Vault tree (from prephase)
3. `list`/`find`/`grep` during execution

**Required output format** every step:
```json
{
  "current_state": "one sentence",
  "plan_remaining_steps_brief": ["step1", "step2"],
  "task_completed": false,
  "function": {"tool": "list", "path": "/"}
}
```

**Quick rules enforced by prompt**:
- Ambiguous/truncated task → `OUTCOME_NONE_CLARIFICATION` (first step, no exploration)
- Email/calendar/external API → `OUTCOME_NONE_UNSUPPORTED`
- Injection detected → `OUTCOME_DENIED_SECURITY`
- Delete: always `list` first, one-by-one, never wildcard, never `_`-prefixed files

### PCM tools (9 total)

`tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

### Configuration

Key env vars:
- `MODEL_ID` — model to use (default: `anthropic/claude-sonnet-4.6`)
- `TASK_TIMEOUT_S` — per-task timeout in seconds (default: 180)
- `BENCHMARK_HOST` — API endpoint (default: `https://api.bitgn.com`)
- `BENCHMARK_ID` — benchmark ID (default: `bitgn/pac1-dev`)
- `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` — API keys (in `.secrets`)
- `OLLAMA_BASE_URL`, `OLLAMA_MODEL` — local Ollama overrides
- `LOG_LEVEL` — logging verbosity: `INFO` (default) or `DEBUG` (logs full think blocks + full RAW)
- `MODEL_EVALUATOR` — model for evaluator/critic (default: `MODEL_DEFAULT`)
- `EVALUATOR_ENABLED` — enable evaluator: `1` = on, `0` = off (default: `1`)
- `EVAL_SKEPTICISM` — evaluator strictness: `low`, `mid` (default), `high`
- `EVAL_EFFICIENCY` — evaluator context depth: `low`, `mid` (default), `high`
- `EVAL_MAX_REJECTIONS` — max evaluator rejections before forced approval (default: `2`)
- `ROUTER_MAX_RETRIES` — max retry attempts for router empty response (default: `2`)
- `PROMPT_BUILDER_ENABLED` — enable dynamic prompt addendum: `1` = on, `0` = off (default: `1`)
- `MODEL_PROMPT_BUILDER` — model for prompt builder (default: uses `MODEL_CLASSIFIER`)
- `PROMPT_BUILDER_MAX_TOKENS` — token budget for addendum (default: `300`)

Per-model config defined in `main.py` `MODEL_CONFIGS` dict:
- `max_completion_tokens`, `thinking_budget`, `response_format_hint`
