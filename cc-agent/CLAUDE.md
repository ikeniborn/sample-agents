# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

**cc-agent** — замена pac1-py агента: вместо Python LLM-цикла используется Claude Code CLI напрямую через MCP. Четыре файла.

## Запуск

```bash
# Из директории cc-agent:
make run           # все задания
make task TASKS="t01 t03"  # конкретные задания
```

Переменные окружения читаются из `cc-agent/.env` и `cc-agent/.secrets`:
- `BITGN_HOST` — API endpoint (default: `https://api.bitgn.com`)
- `BENCH_ID` — benchmark ID (default: `bitgn/pac1-dev`)
- `TASK_TIMEOUT_S` — таймаут на задание (default: `300s`)
- `PARALLEL_TASKS` — параллельность (default: `2`)
- `MULTI_AGENT` — `1` = pipeline Classifier→Executor→Verifier, `0` = legacy single-agent
- `MAX_RETRIES` — max повторов executor при reject от verifier (default: `1`)
- `CLAUDE_MODEL` / `CLAUDE_CLASSIFIER_MODEL` / `CLAUDE_VERIFIER_MODEL` — модели per-role
- `BITGN_API_KEY` — в `.secrets`, включает run mode

## Архитектура

```
runner.py          # оркестратор: bitgn API → spawn iclaude → score
agents.py          # multi-agent prompts, parsers, protocol (Classifier/Executor/Verifier)
mcp_pcm.py         # MCP сервер + harness: guards, stall detection, evaluator, replay log
prompt.py          # адаптивный промпт: classify task → base prompt + addendum
```

**Поток данных (MULTI_AGENT=1):**
1. `start_playground(task_id)` → `harness_url`, `instruction`, `trial_id`
2. **Classifier** (readonly MCP): читает vault, генерирует tailored system prompt + key_rules
3. **Executor** (full MCP): выполняет задание с prompt от Classifier
4. **Verifier** (readonly MCP): проверяет результат → approve/correct/reject (retry MAX_RETRIES)
5. `report_completion(outcome, message, refs)` → evaluator gate → `vm.answer()`
6. `end_trial(trial_id)` → score

**MCP_MODE** per роль: `full` (read+write), `readonly` (tree/find/search/list/read), `draft` (пишет в /tmp)

## MCP инструменты (= PCM tools)

`tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

`report_completion` принимает `outcome`: `ok` | `clarification` | `unsupported` | `security`

## Ограничения

- Не хардкодить значения — прорабатывать логику
