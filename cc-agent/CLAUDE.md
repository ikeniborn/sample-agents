# CLAUDE.md

## Overview

`cc-agent` заменяет Python LLM-цикл на Claude Code CLI через MCP. Четыре модуля — редактируй только их. Конфиг читается из `.env` и `.secrets` (не хардкодить значения).

## Commands

```bash
# Из директории cc-agent:
make run                       # все задания
make task TASKS="t01 t03"      # конкретные задания
```

Env-файлы: `cc-agent/.env`, `cc-agent/.secrets`

| Переменная | Default | Описание |
|---|---|---|
| `BITGN_HOST` | `https://api.bitgn.com` | API endpoint |
| `BENCH_ID` | `bitgn/pac1-dev` | benchmark ID |
| `TASK_TIMEOUT_S` | `300s` | таймаут на задание |
| `PARALLEL_TASKS` | `2` | параллельность |
| `MULTI_AGENT` | — | `1` = Classifier→Executor→Verifier, `0` = single-agent |
| `MAX_RETRIES` | `1` | повторов executor при reject |
| `CLAUDE_MODEL` / `CLAUDE_CLASSIFIER_MODEL` / `CLAUDE_VERIFIER_MODEL` | — | модели per-role |
| `CLAUDE_EFFORT` / `CLAUDE_CLASSIFIER_EFFORT` / `CLAUDE_VERIFIER_EFFORT` | — | thinking effort per-role (low/medium/high/max) |
| `BITGN_API_KEY` | `.secrets` | включает run mode |

## Architecture

```
runner.py     # оркестратор: bitgn API → spawn iclaude → score
agents.py     # prompts, parsers, protocol (Classifier/Executor/Verifier)
mcp_pcm.py    # MCP сервер + harness: guards, stall detection, evaluator
prompt.py     # адаптивный промпт: classify task → base prompt + addendum
```

## Data Flow

`MULTI_AGENT=1` pipeline:

1. `start_playground(task_id)` → `harness_url`, `instruction`, `trial_id`
2. **Classifier** (readonly MCP): читает vault, генерирует system prompt + key_rules
3. **Executor** (full MCP): выполняет задание
4. **Verifier** (readonly MCP): approve / correct / reject (retry до `MAX_RETRIES`)
5. `report_completion(outcome, message, refs)` → evaluator gate → `vm.answer()`
6. `end_trial(trial_id)` → score

## Tools

MCP tools: `tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

`report_completion` — `outcome`: `ok` | `clarification` | `unsupported` | `security`

MCP_MODE per роль: `full` (read+write), `readonly` (tree/find/search/list/read), `draft` (пишет в `/tmp`)

## Constraints

- **Не хардкодить** — промт-патчи (`"не делай X"`) запрещены, так как они маскируют системный баг вместо устранения причины.
  - Плохо: добавить ограничение в промт, потому что агент делает X на конкретном кейсе.
  - Хорошо: найти причину в логике классификации, парсинге или архитектуре пайплайна — и исправить там.
- При проектированни и реактировании протов использвоать навык @skill:promt-verifier
