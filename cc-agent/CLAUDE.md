# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

**cc-agent** — замена pac1-py агента: вместо Python LLM-цикла используется Claude Code CLI напрямую через MCP. Три файла (~567 LOC суммарно).

## Запуск

```bash
# Зависимости — из pac1-py (uv sync если не установлены)
cd pac1-py && uv sync

# Все задания
uv run python ../cc-agent/runner.py

# Конкретные задания
uv run python ../cc-agent/runner.py t01 t03
```

Переменные окружения читаются из `pac1-py/.env` и `pac1-py/.secrets`:
- `BENCHMARK_HOST` — API endpoint (default: `https://api.bitgn.com`)
- `BENCHMARK_ID` — benchmark ID (default: `bitgn/pac1-dev`)
- `TASK_TIMEOUT_S` — таймаут на задание (default: `300s`)

## Архитектура

```
runner.py          # оркестратор: bitgn API → start_playground → spawn claude CLI → end_trial
mcp_pcm.py         # MCP сервер (stdio, JSON-RPC 2.0): транслирует вызовы от Claude в PCM gRPC API
prompt.py          # системный промпт для Claude Code (7 правил)
```

**Поток данных на одно задание:**
1. `start_playground(task_id)` → `harness_url`, `instruction`, `trial_id`
2. Записывается temp JSON: `{"mcpServers": {"pcm": {... "env": {"HARNESS_URL": "..."}}}}`
3. Запуск subprocess: `claude --print --mcp-config <cfg> -p "<instruction>"`
4. Claude вызывает MCP tools → `mcp_pcm.py` проксирует в PCM runtime по `harness_url`
5. Claude вызывает `report_completion(outcome, message, refs)` → `vm.answer()`
6. `end_trial(trial_id)` → score

## MCP инструменты (= PCM tools)

`tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

`report_completion` принимает `outcome`: `ok` | `clarification` | `unsupported` | `security`

## Ограничения

- Не трогать `pac1-py/.secrets`
- Не хардкодить значения — прорабатывать логику
