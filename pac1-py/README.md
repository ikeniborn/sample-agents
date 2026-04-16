# pac1-py — PAC-1 Benchmark Agent

Python-агент для бенчмарка PAC-1. Решает задачи с файловым хранилищем (vault) через
инструменты: tree, find, search, list, read, write, delete, mkdir, move, report_completion.

---

## Быстрый старт

```bash
# Установить зависимости
make sync          # или: uv sync

# Запустить все задачи
uv run python main.py

# Запустить конкретные задачи
uv run python main.py t01 t03 t07
```

Ключи API нужно положить в `.secrets` (рядом с `.env`):

```
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...
```

---

## Переменные окружения

### Модели

| Переменная | По умолчанию | Описание |
|---|---|---|
| `MODEL_DEFAULT` | `anthropic/claude-sonnet-4.6` | Основная модель |
| `MODEL_THINK` | `MODEL_DEFAULT` | Модель для задач distill/analyze |
| `MODEL_TOOL` | `MODEL_DEFAULT` | Модель для задач delete/move/rename |
| `MODEL_LONG_CONTEXT` | `MODEL_DEFAULT` | Модель для задач с большим контекстом |
| `MODEL_CODER` | `MODEL_DEFAULT` | Подагент для code_eval |
| `MODEL_CLASSIFIER` | `MODEL_DEFAULT` | Классификатор типа задачи |
| `MODEL_EVALUATOR` | `MODEL_DEFAULT` | Критик (evaluator/critic) |
| `MODEL_PROMPT_BUILDER` | `MODEL_CLASSIFIER` | Генератор addendum (inference) |
| `MODEL_OPTIMIZER` | `MODEL_CLASSIFIER` | Модель для `optimize_prompts.py` |

### Инфраструктура

| Переменная | По умолчанию | Описание |
|---|---|---|
| `BENCHMARK_HOST` | `https://api.bitgn.com` | API-эндпоинт бенчмарка |
| `BENCHMARK_ID` | `bitgn/pac1-dev` | ID бенчмарка |
| `TASK_TIMEOUT_S` | `180` | Таймаут на задачу (секунды) |
| `LOG_LEVEL` | `INFO` | `INFO` или `DEBUG` (полный вывод think-блоков и RAW) |

### Evaluator (критик)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `EVALUATOR_ENABLED` | `1` | `1` — включён, `0` — выключен |
| `EVAL_SKEPTICISM` | `mid` | Строгость проверки: `low` / `mid` / `high` |
| `EVAL_EFFICIENCY` | `mid` | Глубина контекста: `low` / `mid` / `high` |
| `EVAL_MAX_REJECTIONS` | `2` | Максимум отказов до принудительного одобрения |

### Prompt Builder

| Переменная | По умолчанию | Описание |
|---|---|---|
| `PROMPT_BUILDER_ENABLED` | `1` | `1` — включён, `0` — выключен |
| `PROMPT_BUILDER_MAX_TOKENS` | `300` | Бюджет токенов для addendum |

### Провайдеры

| Переменная | Описание |
|---|---|
| `OLLAMA_BASE_URL` | URL локального Ollama (например `http://localhost:11434`) |
| `OLLAMA_MODEL` | Имя модели Ollama |

---

## DSPy — оптимизация промтов

Агент использует [DSPy](https://dspy.ai) для двух подсистем:

- **Prompt Builder** — генерирует task-специфичные подсказки перед основным циклом
  (`dspy.Predict(PromptAddendum)` в `agent/prompt_builder.py`)
- **Evaluator** — проверяет результат агента перед отправкой
  (`dspy.ChainOfThought(EvaluateCompletion)` в `agent/evaluator.py`)

Оба модуля работают через `DispatchLM` — обёртку над существующим `call_llm_raw()`
(3-tier routing: Anthropic → OpenRouter → Ollama). Глобальное состояние DSPy не меняется:
используется `dspy.context(lm=...)` на каждый вызов.

### Как работает цикл данных

```
каждый прогон main.py
  └─► data/dspy_examples.jsonl  ← растёт автоматически (1 запись/задача)

при ≥ 30 записях
  └─► агент печатает: "[dspy] 30 real examples → run: optimize_prompts.py"

optimize_prompts.py
  ├─► читает data/dspy_examples.jsonl  (реальные примеры с score ≥ min-score)
  │   если < 30 — добавляет data/dspy_synthetic.jsonl  (cold-start, статичный)
  └─► пишет data/prompt_builder_program.json / data/evaluator_program.json

следующий запуск агента
  └─► автоматически загружает data/*.json  (fail-open если файл отсутствует)
```

**`data/dspy_synthetic.jsonl` чистить не нужно.** Как только реальных примеров
станет ≥ 30, оптимизатор автоматически перестаёт их использовать.

### Запуск оптимизатора

```bash
# Оптимизировать prompt builder
uv run python optimize_prompts.py --target builder

# Оптимизировать evaluator
uv run python optimize_prompts.py --target evaluator

# Оба сразу
uv run python optimize_prompts.py --target all

# Использовать другую модель для оптимизации
MODEL_OPTIMIZER=anthropic/claude-opus-4-5 uv run python optimize_prompts.py --target all

# Повысить порог качества примеров (по умолчанию 0.8)
uv run python optimize_prompts.py --target builder --min-score 0.9
```

Приоритет модели для оптимизатора: `MODEL_OPTIMIZER` → `MODEL_CLASSIFIER` → `MODEL_DEFAULT`.

Скомпилированные программы:

| Файл | Используется в |
|---|---|
| `data/prompt_builder_program.json` | `agent/prompt_builder.py` при старте |
| `data/evaluator_program.json` | `agent/evaluator.py` при старте |

Если файл отсутствует — модуль работает с промтами по умолчанию (fail-open).

### Сброс оптимизации

```bash
rm data/prompt_builder_program.json data/evaluator_program.json
```

После этого агент снова использует промты из кода.

### Просмотр накопленных примеров

```bash
# Количество примеров
wc -l data/dspy_examples.jsonl

# Примеры с высоким score
python3 -c "
import json
for line in open('data/dspy_examples.jsonl'):
    ex = json.loads(line)
    if ex['score'] >= 0.9:
        print(ex['task_type'], ex['score'], ex['task_text'][:60])
"
```

---

## Архитектура агента

```
main.py → run_agent() [agent/__init__.py]
  ├── run_prephase()           — vault tree + AGENTS.MD
  ├── ModelRouter.resolve()    — классификация типа задачи, выбор модели
  ├── build_system_prompt()    — модульная сборка системного промта
  ├── build_dynamic_addendum() — DSPy: task-специфичные подсказки
  └── run_loop()               — до 30 шагов: LLM → tool → PCM
        ├── evaluator          — DSPy: проверка результата перед submit
        ├── stall detection    — обнаружение зависания
        ├── security gates     — проверки инъекций
        └── log compaction     — сжатие лога
```

### Типы задач

| Тип | Ключевые слова | Переменная модели |
|---|---|---|
| `think` / `distill` | analyze, compare, summarize | `MODEL_THINK` |
| `tool` | delete, move, rename | `MODEL_TOOL` |
| `longContext` | 3+ путей, "all files" | `MODEL_LONG_CONTEXT` |
| `email` | send email, draft | `MODEL_DEFAULT` |
| `inbox` | process inbox | `MODEL_DEFAULT` |
| `lookup` | find, what is | `MODEL_DEFAULT` |
| `coder` | calculate, compute | `MODEL_CODER` |
| `default` | всё остальное | `MODEL_DEFAULT` |

---

## Тесты

```bash
uv run pytest tests/

# Конкретный файл
uv run pytest tests/test_evaluator.py -v
```

---

## Replay-трейсер

```bash
# Включить запись трейсов
TRACE_ENABLED=1 uv run python main.py t01

# Посмотреть события
cat logs/*/traces.jsonl | python3 -c "
import sys, json
for l in sys.stdin:
    e = json.loads(l)
    print(e['event'], e.get('task_type',''), e.get('step',''))
"
```
