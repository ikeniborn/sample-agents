# pac1-py — Evaluator (Critic Gate)

Generated: 2026-04-05 | Fix counter: FIX-224 (FIX-225 is next)

## Назначение

Evaluator — качество-гейт, который перехватывает завершение задачи **до** финального `vm.answer()`.
LLM-критик проверяет: совпадает ли заявленный исход с реальными операциями и текстом задачи.

Дизайн: **fail-open** — любая ошибка LLM/парсинга → автоматическое одобрение.

---

## Поток управления

```mermaid
flowchart TD
    A([_run_step вызван]) --> B{job = report_completion?}
    B -- Нет --> Z([dispatch напрямую])
    B -- Да --> C{EVALUATOR_ENABLED\n== 1?}
    C -- Нет --> Z
    C -- Да --> D{outcome ∈\nOK / CLARIFY / DENIED?}
    D -- Нет --> Z
    D -- Да --> E{eval_rejections <\nMAX_REJECTIONS?}
    E -- Нет --> Z
    E -- Да --> F{осталось > 30 сек?}
    F -- Нет --> Z
    F -- Да --> G[_filter_superseded_ops\nудалить WRITTEN→DELETED]
    G --> H[evaluate_completion\nLLM-вызов]
    H --> I{EvalVerdict\napproved?}
    I -- True --> J([dispatch: vm.answer])
    I -- False --> K[eval_rejections++\nappend correction_hint → log]
    K --> L([return False\nпродолжить loop])
```

---

## Детальный поток evaluator

```mermaid
sequenceDiagram
    participant Step as _run_step
    participant Filter as _filter_superseded_ops
    participant Eval as evaluator.py
    participant LLM as call_llm_raw
    participant PCM as vm.answer

    Step->>Step: detect report_completion + outcome
    Step->>Filter: done_ops (список WRITTEN/DELETED)
    Filter-->>Step: ops без superseded WRITTEN

    Step->>Eval: evaluate_completion(task, type, report,\ndone_ops, digest_str, skepticism, efficiency)
    Eval->>Eval: _build_eval_prompt()
    Note over Eval: system_prompt содержит:<br/>скептицизм-директиву<br/>схему ответа approved/issues/correction_hint<br/>триггеры отказа<br/>допустимые OUTCOME_* коды

    Eval->>LLM: call_llm_raw(system, user, max_tokens=256/512/1024)
    LLM-->>Eval: raw JSON string

    alt Пустой ответ или исключение
        Eval-->>Step: EvalVerdict(approved=True) fail-open
    else JSON parse error
        Eval->>Eval: bracket extraction fallback
        alt JSON не найден
            Eval-->>Step: EvalVerdict(approved=True) fail-open
        else JSON распарсен
            Eval-->>Step: EvalVerdict.model_validate(parsed)
        end
    else Успех
        Eval-->>Step: EvalVerdict(approved, issues, correction_hint)
    end

    alt approved == True
        Step->>PCM: vm.answer(outcome, message, refs)
    else approved == False
        Step->>Step: append EVALUATOR correction_hint
        Step-->>Step: return False, продолжить цикл
    end
```

---

## Конфигурация

| Env var | По умолчанию | Допустимые значения | Описание |
|---------|-------------|---------------------|----------|
| `EVALUATOR_ENABLED` | `"0"` | `"0"` / `"1"` | Включить evaluator |
| `MODEL_EVALUATOR` | = `MODEL_DEFAULT` | любой model id | Модель для evaluator |
| `EVAL_SKEPTICISM` | `"mid"` | `low` / `mid` / `high` | Строгость проверки |
| `EVAL_EFFICIENCY` | `"mid"` | `low` / `mid` / `high` | Глубина контекста |
| `EVAL_MAX_REJECTIONS` | `"2"` | целое число | Макс. отказов до принудительного пропуска |

### Уровни скептицизма

```mermaid
graph LR
    L["low\nОдобряй, если нет\nочевидного противоречия"]
    M["mid\nПроверь outcome vs evidence.\nДата, операции, коды"]
    H["high\nПредполагай ошибку.\nОдобряй только при полном соответствии"]

    L --> M --> H
```

### Уровни efficiency → токены + контекст

| Level | max_tokens | Включено в user_msg |
|-------|-----------|---------------------|
| `low` | 256 | TASK + PROPOSED_OUTCOME + AGENT_MESSAGE |
| `mid` | 512 | + SERVER_DONE_OPS + AGENT_REPORTED_OPS + COMPLETED_STEPS |
| `high` | 1024 | + STEP_DIGEST (полный дайджест шагов) |

---

## Модели данных

```mermaid
classDiagram
    class EvalVerdict {
        +bool approved
        +list~str~ issues
        +str correction_hint
    }

    class _LoopState {
        +int eval_rejections
        +int evaluator_call_count
        +int evaluator_total_ms
        +str task_text
        +str evaluator_model
        +dict evaluator_cfg
        +list~str~ done_ops
    }

    class ReportTaskCompletion {
        +list~str~ completed_steps_laconic
        +str message
        +list~str~ grounding_refs
        +str outcome
        +list~str~ done_operations
    }

    _LoopState --> EvalVerdict : получает
    _LoopState --> ReportTaskCompletion : содержит job
```

---

## _filter_superseded_ops

Удаляет `WRITTEN` записи для путей, которые позже были `DELETED`.

```mermaid
flowchart LR
    IN["done_ops = [ WRITTEN: /a.txt, DELETED: /a.txt, WRITTEN: /b.txt ]"]
    -->
    F["удалить WRITTEN если путь есть в DELETED"]
    -->
    OUT["result = [ DELETED: /a.txt, WRITTEN: /b.txt ]"]
```

**Почему важно (FIX-223):** Evaluator ранее отклонял OUTCOME_OK, видя `WRITTEN: /file` + `DELETED: /file` — трактовал как «файл создан, но не удалён». Фильтр устраняет ложные отказы.

---

## Какие outcomes проверяются

```mermaid
graph TD
    O{outcome}
    O -->|OUTCOME_OK| E[Evaluator запускается]
    O -->|OUTCOME_NONE_CLARIFICATION| E
    O -->|OUTCOME_DENIED_SECURITY| E
    O -->|OUTCOME_NONE_UNSUPPORTED| S[Пропускается\nдиспатч напрямую]
    O -->|OUTCOME_ERR_INTERNAL| S

    E --> EV[evaluate_completion]
```

**Исключение (FIX-224):** inbox-задачи могут получить OUTCOME_OK с пустым `SERVER_DONE_OPS` — ответ в `report.message` без мутаций файлов считается валидным.

---

## Интеграция в _run_step

```mermaid
flowchart TD
    S1[LLM вызов + stall retry] --> S2[log action]
    S2 --> S3[pre-dispatch guards\nwrite-scope / empty-path]
    S3 --> S4{Evaluator gate\nусловия выполнены?}
    S4 -- Да --> S5[evaluate_completion]
    S5 --> S6{approved?}
    S6 -- Нет --> S7[inject correction_hint\nreturn False]
    S6 -- Да --> S8[dispatch\nvm.answer]
    S4 -- Нет --> S8
```

---

## Статистика в итоговой таблице

`main.py` выводит после каждой задачи:

```
eval_calls=N  eval_rejections=N  eval_ms=NNNms
```

Поля берутся из `_LoopState`: `evaluator_call_count`, `eval_rejections`, `evaluator_total_ms`.

---

## Файлы

| Файл | Роль |
|------|------|
| `agent/evaluator.py` | `evaluate_completion()`, `_build_eval_prompt()`, `EvalVerdict` |
| `agent/loop.py:40-47` | Env vars: `_EVALUATOR_ENABLED`, `_EVAL_SKEPTICISM`, `_EVAL_EFFICIENCY`, `_MAX_EVAL_REJECTIONS` |
| `agent/loop.py:942-947` | `_filter_superseded_ops()` |
| `agent/loop.py:1624-1657` | Evaluator gate в `_run_step()` |
| `agent/classifier.py:297` | `ModelRouter.evaluator` поле |
| `agent/__init__.py:37-43` | Резолюция `evaluator_model` + `evaluator_cfg` |
