# cc-agent — Claude Code as pac1 agent

Вместо Python-агента с LLM-циклом — Claude Code CLI напрямую через MCP.

## Архитектура

```
runner.py          # оркестратор: bitgn API → spawn iclaude → score
mcp_pcm.py         # MCP сервер + harness: guards, stall detection, evaluator, replay log
prompt.py          # адаптивный промпт: classify task → base prompt + addendum
```

```
runner.py
  ├── bitgn API → get tasks → start_playground → harness_url
  ├── classify_task(instruction) → адаптивный system prompt
  ├── пишет tmp MCP config (mcpServers.pcm → mcp_pcm.py + HARNESS_URL)
  ├── запускает: iclaude --print --mcp-config cfg.json --system-prompt <prompt> <instruction>
  │     └── Claude Code использует MCP инструменты (tree/list/read/write/...)
  │           └── mcp_pcm.py проксирует вызовы в PCM runtime (harness_url)
  │                 ├── write/delete guards (AGENTS.MD, _prefix, docs/channels/)
  │                 ├── injection detection при read
  │                 ├── stall detection (повтор 3x, 12 шагов без мутации)
  │                 └── evaluator gate перед report_completion
  └── end_trial → score
```

## Инструменты MCP (= PCM tools)

`tree`, `find`, `search`, `list`, `read`, `write`, `delete`, `mkdir`, `move`, `report_completion`

## Быстрый старт

### 1. Зависимости

```bash
cd pac1-py && uv sync
```

### 2. Конфигурация

```bash
# Скопировать шаблон
cp .env.example .env

# Создать файл секретов
echo "BITGN_API_KEY=<ваш_ключ>" > .secrets
```

### 3. Запуск

```bash
# Из директории cc-agent:
make run           # все задания
make task TASKS="t01 t03"  # конкретные задания

# Или напрямую через uv (из pac1-py):
cd ../pac1-py && uv run python ../cc-agent/runner.py
```

## Конфигурация

Переменные окружения читаются из `cc-agent/.env` и `cc-agent/.secrets` (shell env имеет приоритет):

| Переменная | Default | Описание |
|-----------|---------|----------|
| `BITGN_HOST` | `https://api.bitgn.com` | API endpoint |
| `BENCH_ID` | `bitgn/pac1-dev` | Benchmark ID |
| `TASK_TIMEOUT_S` | `300` | Таймаут на задание (сек) |
| `PARALLEL_TASKS` | `1` | Параллельные задания |
| `ICLAUDE_CMD` | `iclaude` | Команда запуска Claude Code CLI (путь или `bash /path/to/iclaude.sh`) |
| `BITGN_API_KEY` | — | API ключ (`.secrets`), включает run mode |
| `BITGN_RUN_NAME` | — | Метка запуска на leaderboard |

## Harness-слои (mcp_pcm.py)

| Слой | Описание |
|------|----------|
| **Write guard** | Блокирует запись в AGENTS.MD, docs/channels/ (кроме otp.txt) |
| **Delete guard** | Блокирует удаление `_`-префиксных файлов и защищённых путей |
| **Injection scan** | NFKC-нормализация + regex-детекция инъекций при read |
| **Stall detection** | Fingerprint повтора 3x, 12+ шагов без мутации → `[SYSTEM HINT]` |
| **Evaluator gate** | Проверка outcome vs evidence перед report_completion (soft, trace only) |
| **Replay log** | Структурированный JSON trace: steps[], args, elapsed_ms, outcome |

## Отличия от pac1-py

| | pac1-py | cc-agent |
|---|---|---|
| Агент | Python + LLM-цикл (30 шагов) | Claude Code CLI |
| Инструменты | PCM клиент напрямую | PCM через MCP |
| Промпт | Модульный prompt builder (8 типов) | Адаптивный: base + 4 addenda (regex classify) |
| Harness | loop.py 2600+ строк | mcp_pcm.py ~530 строк (guards + stall + eval + replay) |
| Конфигурация | pac1-py/.env + .secrets | Собственные cc-agent/.env + .secrets |
