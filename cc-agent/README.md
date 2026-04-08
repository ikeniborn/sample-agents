# cc-agent — Claude Code as pac1 agent

Вместо Python-агента с LLM-циклом — Claude Code CLI напрямую через MCP.

## Архитектура

```
runner.py
  ├── bitgn API → get tasks → start_playground → harness_url
  ├── пишет tmp MCP config (mcpServers.pcm → mcp_pcm.py + HARNESS_URL)
  ├── запускает: claude --print --mcp-config cfg.json -p "<instruction>"
  │     └── Claude Code использует MCP инструменты (tree/list/read/write/...)
  │           └── mcp_pcm.py проксирует вызовы в PCM runtime (harness_url)
  └── end_trial → score
```

## Инструменты MCP (= PCM tools)

`tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

## Запуск

```bash
# Из директории cc-agent/
# (зависимости берутся из pac1-py/pyproject.toml через PYTHONPATH)

cd cc-agent

# Все задания
python runner.py

# Конкретные задания
python runner.py t01 t03
```

Переменные окружения те же, что у pac1-py (читаются из `pac1-py/.env` и `pac1-py/.secrets`):
- `BENCHMARK_HOST` — API endpoint
- `BENCHMARK_ID` — benchmark ID
- `TASK_TIMEOUT_S` — таймаут на задание (default: 300s)

## Как работает

1. `runner.py` подключается к `api.bitgn.com`, берёт список заданий
2. На каждое задание:
   - `start_playground` → получает `harness_url` и `instruction`
   - Записывает temp JSON с MCP конфигом: `{"mcpServers": {"pcm": {..., "env": {"HARNESS_URL": "..."}}}}`
   - Запускает `claude --print --mcp-config <cfg> -p "<instruction>"`
   - Claude Code думает и вызывает MCP tools (tree/list/read/write/...)
   - `mcp_pcm.py` (subprocess, stdio) транслирует вызовы в PCM API
   - Когда задание выполнено, Claude вызывает `report_completion`
   - `mcp_pcm.py` вызывает `vm.answer()` на сервере
3. После завершения `claude`: `end_trial` → оценка

## Отличия от pac1-py

| | pac1-py | cc-agent |
|---|---|---|
| Агент | Python + LLM-цикл (30 шагов) | Claude Code CLI |
| Инструменты | PCM клиент напрямую | PCM через MCP |
| Промпт | Сложный prompt.py (stall detection, evaluator) | Минимальный system prompt |
| Логика | loop.py 2600+ строк | runner.py ~120 строк |
