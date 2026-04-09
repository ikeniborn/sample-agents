# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

**cc-agent** — замена pac1-py агента: вместо Python LLM-цикла используется Claude Code CLI напрямую через MCP. Три файла (~740 LOC суммарно).

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
- `BITGN_API_KEY` — в `.secrets`, включает run mode

## Архитектура

```
runner.py          # оркестратор: bitgn API → spawn iclaude → score
mcp_pcm.py         # MCP сервер + harness: guards, stall detection, evaluator, replay log
prompt.py          # адаптивный промпт: classify task → base prompt + addendum
```

**Поток данных на одно задание:**
1. `start_playground(task_id)` → `harness_url`, `instruction`, `trial_id`
2. `classify_task(instruction)` → адаптивный system prompt
3. Записывается temp JSON: `{"mcpServers": {"pcm": {... "env": {"HARNESS_URL": "...", "TASK_ID": "...", "TASK_INSTRUCTION": "..."}}}}`
4. Запуск subprocess: `iclaude --print --mcp-config <cfg> --system-prompt <prompt> <instruction>`
5. Claude вызывает MCP tools → `mcp_pcm.py` проксирует в PCM runtime по `harness_url`
6. mcp_pcm.py применяет harness: guards, stall detection, injection scan
7. Claude вызывает `report_completion(outcome, message, refs)` → evaluator gate → `vm.answer()`
8. `end_trial(trial_id)` → score

## MCP инструменты (= PCM tools)

`tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

`report_completion` принимает `outcome`: `ok` | `clarification` | `unsupported` | `security`

## Ограничения

- Не хардкодить значения — прорабатывать логику
