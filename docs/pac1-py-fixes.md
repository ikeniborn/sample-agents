# pac1-py Agent — Applied Fixes

> Дата: 2026-03-24
> Агент: `pac1-py/agent/` (PAC1 benchmark, PCM runtime)
> Результат: **100% на bitgn/pac1-dev** (anthropic/claude-haiku-4.5, qwen/qwen3.5-9b)

---

## Применённые фиксы

### loop.py

| ID | Строки | Описание |
|----|--------|---------|
| **FIX-27** | 100–140 | Retry-loop (4 попытки, 4s sleep) на transient-ошибки: `503`, `502`, `NoneType`, `overloaded`, `unavailable`, `server error` от OpenRouter/провайдеров |
| **FIX-qwen** | 98, 105–120 | `use_json_object=True` в cfg → `response_format={"type":"json_object"}` вместо Pydantic structured output. Нужен для qwen: structured-режим вызывает token-blowout (10000+ токенов на вывод схемы) |
| **JSON-correction-retry** | 142–158 | После FIX-qwen: если `model_validate_json` провалился — инжектирует correction-hint в лог, делает ещё 1 попытку, затем убирает hint (успех или нет) |
| **FIX-63** | 184–195 | Auto-list родительской директории перед первым `delete` из неё. Предотвращает удаление "вслепую" без знания содержимого папки |
| **DELETED/WRITTEN feedback** | 207–212 | После `delete`/`write`/`mkdir` — вместо сырого proto-JSON возвращает `DELETED: <path>` / `WRITTEN: <path>` / `CREATED DIR: <path>`. Предотвращает повторные удаления после log-компакции (модель "забывает" что уже сделала) |
| **Log compaction** | 47–69, 92 | Скользящее окно: `preserve_prefix` (system + task + prephase) никогда не сжимается; хвост — последние 5 пар assistant/tool; старые пары заменяются кратким summary из last-5 assistant-сообщений |
| **max_steps=30** | 82 | Лимит 30 шагов (не 20) — PAC1-задачи требуют больше шагов (list + read + find + write) |

### prephase.py

| ID | Строки | Описание |
|----|--------|---------|
| **Discovery-first prephase** | 33–101 | До main loop: `tree /` + чтение `AGENTS.MD` (кандидаты: `/AGENTS.MD`, `/AGENTS.md`, `/02_distill/AGENTS.md`). Результат инжектируется в контекст как `preserve_prefix` — никогда не компактируется. Агент получает полную карту vault до первого шага |

### main.py / MODEL_CONFIGS

| ID | Строки | Описание |
|----|--------|---------|
| **MODEL_CONFIGS** | 15–18 | `qwen/qwen3.5-9b`: `max_completion_tokens=4000`, `use_json_object=True`. `anthropic/claude-haiku-4.5`: пустой конфиг (structured output работает нативно) |
| **Итоговая статистика** | 83–95 | Таблица в stdout по завершению: task_id, score, elapsed, проблемы — для сбора логов по CLAUDE.md |

---

## Архитектурные решения (не нумерованные фиксы)

### Discovery-first промпт (prompt.py)

Системный промпт содержит **ноль хардкодных путей vault**. Вся информация о папках поступает из:
1. AGENTS.MD (pre-loaded в prephase)
2. Дерева vault (pre-loaded в prephase)
3. `list`/`find`/`search` вызовов в процессе выполнения задачи

Ключевые правила промпта:
- Каждый путь должен прийти из `list`/`find`/`tree` результата — не конструировать из памяти
- Шаблонные файлы (`_*` или помеченные в AGENTS.MD) — никогда не удалять
- "Keep the diff focused": выполнить все явно запрошенные операции, затем сразу `report_completion`
- Перед записью производного файла — list целевой директории для проверки существования
- Вместо `ask_clarification` — `report_completion` с `OUTCOME_NONE_CLARIFICATION`

### VaultContext — заменён неявным подходом

`VaultContext` (`models.py:10–39`) определён, но **не используется нигде в коде** — мёртвый код.

Вместо структурированного извлечения контекста из AGENTS.MD агент использует:
- **Неявный подход**: полный текст AGENTS.MD + tree инжектируется в контекст LLM как есть
- LLM самостоятельно интерпретирует содержимое AGENTS.MD и определяет роли папок
- Никакого программного парсинга AGENTS.MD нет — только prompt-инструкции

Это работает для claude и qwen-9b, но менее надёжно для слабых моделей.

---

## Ограничения OpenRouter / JSON

### Structured output (Pydantic parse mode)
- `client.beta.chat.completions.parse(response_format=NextStep, ...)` работает только если провайдер поддерживает structured output
- OpenRouter передаёт это провайдеру — **не все провайдеры поддерживают**
- qwen-модели через OpenRouter/Together: structured output вызывает **token-blowout** (модель начинает выводить JSON Schema вместо ответа)
- Решение: `use_json_object=True` → `response_format={"type":"json_object"}` + ручной `model_validate_json`

### json_object режим
- Гарантирует валидный JSON, **но не гарантирует соответствие схеме**
- Поля могут отсутствовать или иметь неверный тип → `ValidationError` → JSON-correction-retry
- Провайдеры **могут игнорировать** `max_completion_tokens` (задокументировано в MEMORY.md)

### Transient-ошибки (FIX-27)
- OpenRouter провайдеры (Venice/Together) имеют **503/502 storms** в часы пик
- `NoneType` ошибки — модель вернула пустой ответ
- Решение: retry 4 раза с 4s sleep, после чего abort

### Итог по json_object vs structured
| Режим | Claude | qwen-9b | qwen-4b/2b |
|-------|--------|---------|------------|
| structured (Pydantic) | ✅ работает | ❌ token-blowout | ❌ token-blowout |
| json_object | ✅ работает | ✅ работает | ✅ работает (с retry) |

---

## Что не применено / мёртвый код

| Элемент | Файл | Статус |
|---------|------|--------|
| `VaultContext` | `models.py:10–39` | Определён, нигде не используется |
| Все sandbox-фиксы (Fix-21–62b) | — | Отсутствуют — их заменяет discovery-first архитектура |
