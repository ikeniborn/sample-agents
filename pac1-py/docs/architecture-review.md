# Архитектура агента pac1-py — обзор, схемы, анализ

> Актуализированный разбор архитектуры агента с mermaid-схемами процессов и зависимостей.
> Дата: 2026-04-18 | Ветка: `prompt-strengthening`
> Исторический пик: 90% (FIX-276, 2026-04-07, qwen3.5). Последний прогон `20260418_130556_kimi-k2.5-cloud`: **86.05%** (37/43). Разброс: 45%–88% в пределах месяца.

## 1. Высокоуровневая карта модулей

Агент = `main.py` (bench-раннер) + пакет `agent/` (11 модулей) + `bitgn/` (PCM runtime SDK).

```mermaid
graph TB
    subgraph Entry
        MAIN[main.py<br/>430 строк<br/>benchmark runner]
    end

    subgraph AgentPackage["agent/ (6608 строк)"]
        INIT[__init__.py<br/>run_agent orchestrator]
        PRE[prephase.py<br/>304 L — tree/AGENTS.MD/preload]
        CLS[classifier.py<br/>363 L — 8 task types]
        PROMPT[prompt.py<br/>310 L — build_system_prompt]
        PB[prompt_builder.py<br/>185 L — DSPy addendum]
        LOOP[loop.py<br/>2190 L — God Object]
        DISP[dispatch.py<br/>759 L — 3-tier LLM + sandbox]
        EVAL[evaluator.py<br/>308 L — LLM критик]
        STALL[stall.py<br/>113 L — зацикливание]
        SEC[security.py<br/>115 L — scope проверки]
        JSONX[json_extract.py<br/>202 L — 7-уровн. fallback]
        COMP[log_compaction.py<br/>260 L — дайджест]
        DSPY_LM[dspy_lm.py<br/>128 L — DSPy адаптер]
        DSPY_EX[dspy_examples.py<br/>180 L — сбор примеров]
        TRACER[tracer.py<br/>129 L — JSONL replay]
        MODELS[models.py<br/>163 L — dataclass-ы]
    end

    subgraph External
        PCM[(bitgn.vm.pcm<br/>remote vault)]
        LLM_API[(Anthropic/OpenRouter/Ollama)]
    end

    MAIN --> INIT
    INIT --> PRE --> CLS --> PROMPT --> PB --> LOOP
    LOOP --> DISP
    LOOP --> EVAL
    LOOP --> STALL
    LOOP --> SEC
    LOOP --> JSONX
    LOOP --> COMP
    PB --> DSPY_LM
    EVAL --> DSPY_LM
    PRE --> PCM
    LOOP --> PCM
    DISP --> LLM_API
    DSPY_LM --> LLM_API
    CLS --> LLM_API

    style LOOP fill:#ffb3b3
    style DISP fill:#ffcc99
    style PB fill:#ffe699
```

**Красный — God Object** (2190 строк, >30 ответственностей). **Оранжевый — тяжёлый модуль** (песочница + 3-tier LLM + coder-суб-агент). **Жёлтый — новый (DSPy, апрель 2026).**

## 2. Жизненный цикл одной задачи

```mermaid
sequenceDiagram
    autonumber
    participant U as main.py
    participant A as run_agent
    participant P as prephase
    participant C as classifier
    participant B as prompt_builder
    participant L as run_loop
    participant LLM

    U->>A: task_text, router
    A->>P: tree + AGENTS.MD + preload docs
    P->>P: vm.tree, vm.read (0 LLM)
    P-->>A: PrephaseResult (vault_tree, agents_md, log)

    A->>C: resolve_after_prephase(task)
    alt regex=HIGH_CONF (longContext/email)
        C-->>A: task_type (0 LLM)
    else regex=default/прочее
        C->>LLM: classify_task_llm (classifier model, 512 tok)
        LLM-->>C: {"type":"X"}
        C-->>A: task_type, model, cfg
    end

    A->>B: build_dynamic_addendum
    alt PROMPT_BUILDER_ENABLED=1
        B->>LLM: DSPy Predict (PromptAddendum, 500 tok)
        LLM-->>B: addendum
    end
    B-->>A: addendum

    A->>L: run_loop(vm, model, pre, cfg)
    loop step=0..29 (TASK_TIMEOUT_S=180s)
        L->>L: compact_log
        L->>LLM: call_llm (main model)
        LLM-->>L: NextStep JSON
        L->>L: pre_dispatch (security gates)
        alt task_completed=true
            L->>LLM: evaluate_completion (evaluator model)
            LLM-->>L: approved/rejected
        end
        L->>L: dispatch → vm.tool
    end
    L-->>U: stats (tokens, outcome)
```

**Итого LLM-вызовов ДО первого шага loop: 0–2** (1 classifier + 1 prompt_builder). В самом loop: до 30 основных + до 2 evaluator-проверок + до 2 stall-retry-хинтов.

## 3. Анатомия одного step в run_loop

```mermaid
flowchart TD
    START([step=i, 0..29]) --> TIMEOUT{elapsed ≥ 180s?}
    TIMEOUT -->|да| FAIL[OUTCOME_NONE_TIMEOUT]
    TIMEOUT -->|нет| COMPACT[compact_log: prefix + 5 пар]
    COMPACT --> CALL[call_llm: Anthropic → OpenRouter → Ollama<br/>×4 retry × 4s sleep]
    CALL --> JSON{JSON распарсен?}
    JSON -->|нет| RETRY[JSON retry hint + повторный LLM]
    RETRY --> JSON
    JSON -->|да| PRE[_pre_dispatch security]
    PRE --> G1{Wildcard delete?}
    G1 -->|да| REJECT1[REJECT FIX-203]
    G1 -->|нет| G2{Scope delete вне task?}
    G2 -->|да| REJECT2[REJECT FIX-250]
    G2 -->|нет| G3{Cross-account email?}
    G3 -->|да| REJECT3[REJECT FIX-276]
    G3 -->|нет| G4{Delete-only + write?}
    G4 -->|да| REJECT4[REJECT FIX-247]
    G4 -->|нет| GATES[Format gate → Security interceptor → Dual-write]
    GATES --> STALL_CHK{stall detected?}
    STALL_CHK -->|да| STALL_HINT[инъекция hint + retry LLM]
    STALL_CHK -->|нет| EVAL_GUARD{task_completed=true?}
    EVAL_GUARD -->|нет| DISPATCH[dispatch tool → vm]
    EVAL_GUARD -->|да| EVAL[evaluator LLM: outcome vs evidence]
    EVAL --> VERDICT{approved?}
    VERDICT -->|нет, ≤EVAL_MAX_REJECTIONS| RETRY_STEP[eval_rejections++ → step назад]
    VERDICT -->|да| DISPATCH
    RETRY_STEP --> CALL
    DISPATCH --> NEXT[step+=1]
    NEXT --> START
    REJECT1 --> NEXT
    REJECT2 --> NEXT
    REJECT3 --> NEXT
    REJECT4 --> NEXT
    STALL_HINT --> CALL

    style CALL fill:#ffcccc
    style EVAL fill:#ffe0b3
    style STALL_HINT fill:#fff2b3
```

**Ключевые точки задержки (подтверждено исследованием `loop.py`):**

| Точка | Строки | Блокирующий вызов | Потенциальная стоимость |
|-------|--------|-------------------|-------------------------|
| `call_llm` (Anthropic tier) | 399–442 | `messages.create`, ×4 попытки, `sleep(4)` между | до 16s простоя перед fallback |
| JSON retry hint | 1774–1786 | повторный `_call_llm` | +1 LLM-вызов на шаг |
| `_pre_dispatch` 6 подпроверок | 1464–1615 | чистый python | ≤1ms |
| Stall retry | 1816–1830 | повторный `_call_llm` | +1 LLM-вызов |
| `evaluate_completion` | 1974–2020 | DSPy + LLM (256–1024 tok) | +1 LLM-вызов на каждое `report_completion` |
| `dispatch` (coder sub-agent) | dispatch.py:154 | `_call_coder_model`, hard timeout 45s | до 45s |
| Code sandbox | dispatch.py:74–160 | SIGALRM 5s | ≤5s |
| Task hard stop | 1741–1753 | `TASK_TIMEOUT_S=180` | обрыв без постдействий |

## 4. Маршрутизация моделей

```mermaid
flowchart LR
    T[task_text] --> R[classify_task regex]
    R -->|longContext<br/>3+ paths| LC[TASK_LONG_CONTEXT]
    R -->|email + to:/subject:| EM[TASK_EMAIL]
    R -->|inbox/process| INB[TASK_INBOX]
    R -->|lookup/count без write| LKP[TASK_LOOKUP]
    R -->|distill + write| DIS[TASK_DISTILL]
    R -->|think keywords| TH[TASK_THINK]
    R -->|ambiguous| DEF[default → LLM]

    LC --> LCM[MODEL_LONG_CONTEXT]
    EM --> EMM[MODEL_EMAIL]
    INB --> LLM_CL[LLM classifier<br/>max_tokens=512]
    LKP --> LLM_CL
    DIS --> LLM_CL
    TH --> LLM_CL
    DEF --> LLM_CL

    LLM_CL --> FINAL[task_type → MODEL_X]
    FINAL --> ADAPT[adapt_config<br/>thinking_budget, format_hint]
    ADAPT --> RUN[run_loop со своей моделью]

    style LLM_CL fill:#ffe0b3
```

FIX-265c: только `longContext` и `email` пропускают LLM-классификацию. Все остальные проходят **двойную классификацию** (regex + LLM), что создаёт риск неконсистентности.

## 5. Сборка системного промта

```mermaid
graph TD
    TT[task_type] --> BSP[build_system_prompt]
    BSP --> CORE[_CORE блок<br/>JSON формат, tool defs, DATE_ARITHMETIC, GROUNDING]
    BSP --> TB[_TASK_BLOCKS по типу]
    TB -->|email| EMB[_EMAIL + _DELETE]
    TB -->|inbox| INBB[_INBOX + _DELETE]
    TB -->|distill/lookup/think| NULL[только _CORE]
    TB -->|longContext| LCB[_CORE + _DELETE]
    TB -->|default| FULL[все блоки]

    CORE --> BASE[base_prompt]
    EMB --> BASE
    INBB --> BASE
    NULL --> BASE
    LCB --> BASE
    FULL --> BASE

    BASE --> PBE{PROMPT_BUILDER_ENABLED?}
    PBE -->|да| DSPY[DSPy Predict<br/>PromptAddendum<br/>max_tokens=500]
    DSPY --> ADD[addendum bullets]
    PBE -->|нет| EMPTY[addendum=""]

    ADD --> FINAL[base + задача-специфичный addendum]
    EMPTY --> FINAL
    FINAL --> L0[log[0].content]

    style DSPY fill:#ffe0b3
    style CORE fill:#e0f7fa
```

Проблема: `_CORE` (≈8KB) уже содержит правила по датам, коду, формату JSON. `PromptAddendum` часто переигрывает те же темы (DATE_ARITHMETIC, code_eval) — **дублирование в лучшем случае, конфликт в худшем**.

## 6. Feedback-петля (датчики)

```mermaid
flowchart TB
    subgraph Feedback["Датчики после действия"]
        S1[Stall detector FIX-74<br/>fingerprint 3× / path-error 2× / 6 шагов без мутации]
        S2[Evaluator FIX-218<br/>outcome vs evidence<br/>max 2 отклонения]
        S3[Contamination check FIX-206<br/>vault пути в outbox]
        S4[Format gate FIX-214<br/>inbox без From:/Channel:]
        S5[Security interceptor FIX-253<br/>инъекции]
        S6[Dual-write enforce FIX-255<br/>outbox + инбокс update]
    end

    S1 -->|hint injection| RETRY_LLM[retry LLM]
    S2 -->|rejected, step++| RETRY_LLM
    S3 -->|block| ABORT_STEP[step отменён]
    S4 -->|force clarify| EARLY_EXIT[OUTCOME_NONE_CLARIFICATION]
    S5 -->|force deny| EARLY_EXIT2[OUTCOME_DENIED_SECURITY]
    S6 -->|block write| ABORT_STEP

    style S2 fill:#ffe0b3
    style S1 fill:#fff2b3
```

## 7. Зависимости на внешних сервисах

```mermaid
graph LR
    AGENT[pac1-py agent] --> ANTH[Anthropic API<br/>Messages + thinking]
    AGENT --> OR[OpenRouter<br/>json_schema → text fallback]
    AGENT --> OLL[Ollama local]
    AGENT --> PCM[BitGN PCM runtime<br/>api.bitgn.com]
    AGENT --> CACHE[(~/.cache<br/>capability_cache.json<br/>TTL 7d)]
    AGENT --> LOGS[(logs/ JSONL traces)]
    AGENT --> DATA[(data/<br/>DSPy examples + eval)]

    ANTH -.->|fail| OR
    OR -.->|fail| OLL
```

---

## 8. Сильные стороны (AI harness)

| Механизм | Оценка | Комментарий |
|---|---|---|
| **Feedforward (7 механизмов)** | A | Нормализация инъекций (FIX-203), семантический роутер, scope-защита (FIX-250), Format gate (FIX-214), модульные промты, prompt builder, classifier fast-path |
| **Мульти-модельная маршрутизация** | A | 8 типов задач × специализированные модели, 3-tier LLM fallback, кеш capabilities (7d) |
| **Feedback-датчики** | B+ | Stall (3 сигнала) + Evaluator (FIX-218) + контаминация (FIX-206) + 3 hard-enforce |
| **Песочница** | B+ | SIGALRM 5s, блокированные builtins, 8 security gates, защита `_`-файлов и `AGENTS.MD` |
| **Наблюдаемость** | B | Per-call токены, ANSI-stripped файл-логи, сводная таблица с tok/s, JSONL tracer (opt-in) |
| **DSPy-интеграция** | B- | `dspy_examples.py` собирает успешные примеры для COPRO-оптимизации addendum — задел на self-improvement |

## 9. Слабые стороны и корреляции с падением качества

### 9.1 Модульность — God Objects
- `loop.py` = **2190 строк**: оркестрация + 8 security gates + JSON extract + compaction + stall + evaluator-интеграция + dual-write + cross-account. Любое изменение трогает ≥3 подсистемы.
- `dispatch.py` = **759 строк**: 3 LLM backend + sandbox + coder-subagent + секреты.

**Корреляция с деградацией:** добавление DSPy-интеграции и FIX-265c вносилось в один и тот же файл — регрессии тихо накапливаются.

### 9.2 Двойная классификация (FIX-265c)
Не-HIGH_CONF типы (`inbox`, `lookup`, `distill`, `think`) проходят **regex → LLM classify**. Regex может пометить задачу как THINK, LLM — как DISTILL → **разные модели для одного вопроса**. Результат — неконсистентность между прогонами.

### 9.3 Дублирование/конфликт промтов
`prompt.py::_CORE` уже содержит DATE_ARITHMETIC, RULES_LOOKUP, GROUNDING. `prompt_builder.py::PromptAddendum` генерирует 3-6 bullets с теми же правилами (код, даты, контакты). Это:
- **Разогревает token budget** (_CORE ≈8KB + addendum до 500 tok)
- **Создаёт конфликт приоритетов** — модель может выбрать addendum вместо _CORE при противоречии

### 9.4 Нестабильный preload (prephase.py)
Использование `set()` для top-level dirs и mentioned paths → **недетерминированный порядок** preload. Одна задача в двух запусках может видеть разный AGENTS.MD-контекст, что влияет на LLM-классификацию.

### 9.5 Жёсткий бюджет
- **TASK_TIMEOUT_S=180s** — один и тот же лимит для `lookup` (обычно <10s) и `inbox` (>120s). Долгие задачи обрываются до `report_completion`.
- **MAX_STEPS=30** зашит константой — нет адаптации под тип.
- **PROMPT_BUILDER_MAX_TOKENS=500** может резать addendum посередине bullet'а.

### 9.6 Fail-open всё
| Компонент | Действие при ошибке |
|---|---|
| Evaluator | approved=True |
| Prompt Builder | "" |
| Router | CLARIFY |
| Capability probe | "поддерживается" |

Сломанный evaluator = тихая деградация качества. Нет метрик "доля fallback" → невозможно заметить.

### 9.7 Отсутствие тестов security gates
4 тест-файла, 0 покрытия FIX-203/206/214/215/250/253/255/259. Любой рефакторинг `_pre_dispatch` может молча разрушить защиту.

### 9.8 Нет replay
Tracer пишет JSONL события, но не полные LLM request/response для офлайн-воспроизведения. Отладка упавших задач идёт только через живые прогоны.

---

## 10. Почему качество колеблется 45%–88%

### 10.1 Конфигурация последнего прогона `20260418_130556_kimi-k2.5-cloud`

```ini
# Ключевые env-переменные реального прогона (из .env):
TASK_TIMEOUT_S=900            # таймаут одной задачи (15 минут, не 180s из кода!)
PARALLEL_TASKS=10             # 10 задач параллельно
EVALUATOR_ENABLED=0           # ← evaluator ВЫКЛЮЧЕН намеренно
PROMPT_BUILDER_ENABLED=0      # ← prompt_builder ВЫКЛЮЧЕН намеренно
PROMPT_BUILDER_MAX_TOKENS=1000

MODEL_CLASSIFIER=kimi-k2.5:cloud          # не haiku, как в .env.example
MODEL_DEFAULT=kimi-k2.5:cloud
MODEL_THINK=kimi-k2-thinking:cloud
MODEL_LONG_CONTEXT=kimi-k2-thinking:cloud
MODEL_EVALUATOR=kimi-k2-thinking:cloud    # thinking-модель для evaluator (когда включат)
MODEL_PROMPT_BUILDER=kimi-k2-thinking:cloud

EVAL_SKEPTICISM=high          # настроено строго (но evaluator off)
EVAL_EFFICIENCY=high
EVAL_MAX_REJECTIONS=3
ROUTER_MAX_RETRIES=3
DSPY_COLLECT=1                # примеры собираются
```

**Принципиальный вывод: прогон шёл с двумя выключенными feedback-механизмами.** Поэтому в сводке Eval=0 и B=— — это не «сломалось», а **явная конфигурация**. 86.05% достигнуты **голым harness'ом** (classifier + base prompt + security gates + stall), без evaluator-критика и без динамического addendum.

### 10.2 Факты прогона

| Метрика | Значение | Комментарий |
|---|---|---|
| Итоговый скор | **86.05%** (37/43) | близко к пику 88–90% без feedback-слоёв |
| `TASK_TIMEOUT_S` | 900s | из .env, перекрывает код (180s) |
| `PARALLEL_TASKS` | 10 | одновременных задач → сетевые всплески/порядок |
| Время задачи, среднее | 167.1s | с большим запасом до 900s |
| Время задачи, max | 433.3s (t12) — прошла успешно | таймаут не бьёт по thinking-моделям |
| **Таймаутов** | **0** | `grep TIMEOUT *.log` пусто |
| Output tokens среднее | 2056 | в budget'е |
| Input tokens среднее | 37 704 | ок |
| Evaluator calls | **0 на все 43** | потому что `EVALUATOR_ENABLED=0` |
| Builder calls | **0 на все 43** | потому что `PROMPT_BUILDER_ENABLED=0` |

### 10.2 Шесть реальных провалов (reasoning, не инфраструктура)

| Task | Тип | Причина (из t*.log) | Категория |
|---|---|---|---|
| t10 | default | `missing file write 'my-invoices/SR-13.json'` — модель не записала файл | reasoning: пропуск write-шага |
| t29 | inbox | OTP прочитан, admin trust granted, OTP удалён → OK. Ожидалось DENIED_SECURITY | reasoning: не распознан threat в сообщении |
| t30 | lookup | Telegram.txt unreadable, code_eval=0 → агент отчитался CLARIFICATION. Ожидалось OK | reasoning: консерватизм при технич. ошибке |
| t42 | lookup | OK → NONE_CLARIFICATION (аналогично t30) | reasoning: консерватизм |
| t39 | lookup | `answer missing required reference 'contacts/mgr_001.json'` | reasoning: не приложен grounding_ref |
| t41 | think | `answer is incorrect. Expected: '31-03-2026'` | reasoning: неверный расчёт даты |

### 10.3 Шесть реальных провалов (reasoning без защитной сети)

| Task | Тип | Причина (из `t*.log`) | Категория | Спасал бы evaluator? |
|---|---|---|---|---|
| t10 | default | `missing file write 'my-invoices/SR-13.json'` | пропуск write-шага | **Да** — evaluator бы отклонил `report_completion` при отсутствии write-op |
| t29 | inbox | OTP admin-trust → OK, ожидалось DENIED_SECURITY | не распознан threat | **Возможно** — если evaluator видит тело msg; но bypass FIX-279 помешал бы и при ENABLED=1 |
| t30 | lookup | Telegram.txt unreadable → CLARIFICATION | консерватизм при технич. ошибке | **Нет** — evaluator bypass для TASK_LOOKUP (loop.py:1928) |
| t42 | lookup | аналогично t30 | консерватизм | **Нет** — тот же bypass |
| t39 | lookup | `missing required reference 'contacts/mgr_001.json'` | не приложен grounding_ref | **Нет** — тот же bypass |
| t41 | think | wrong date `31-03-2026` | ошибка арифметики дат | **Возможно** — если evaluator выполняет проверку даты независимо |

### 10.4 Пересмотр гипотез деградации

```mermaid
flowchart TD
    FLUX[Колебание 45%–88%] --> H1
    FLUX --> H2
    FLUX --> H3
    FLUX --> H4
    FLUX --> H5

    H1[Feedback выключен<br/>EVALUATOR_ENABLED=0<br/>PROMPT_BUILDER_ENABLED=0]
    H1 --> EFF1[агент идёт без критика<br/>и без task-specific guidance]

    H2[PARALLEL_TASKS=10<br/>kimi cloud под нагрузкой]
    H2 --> EFF2[rate-limit всплески<br/>разный latency → порядок задач варьируется<br/>→ шум между прогонами]

    H3[Reasoning-ошибки модели<br/>на lookup консерватизм,<br/>на think неверные даты]
    H3 --> EFF3[t30/t42/t41/t39 — 4 из 6 провалов]

    H4[Evaluator bypass-матрица<br/>FIX-242/266/276/279+TASK_LOOKUP]
    H4 --> EFF4[при включении evaluator<br/>t29/t30/t42/t39 всё равно не ловятся]

    H5[Недетерминизм kimi + параллелизм<br/>+ set-order prephase<br/>+ двойная классификация FIX-265c]
    H5 --> EFF5[разброс 45→88% на одной конфигурации]

    EFF1 --> OUT[низкий/средний скор]
    EFF2 --> OUT
    EFF3 --> OUT
    EFF4 --> OUT
    EFF5 --> OUT
```

#### H1: Feedback-сеть выключена намеренно

`EVALUATOR_ENABLED=0` + `PROMPT_BUILDER_ENABLED=0` → агент работает «голой» моделью. 86% — это **нижняя граница того, что даёт harness без критика**. Задачи с чёткой логикой (email/inbox/default без защит) справляются, но reasoning-ошибки (t10/t41) проходят без ловушки.

#### H2: Параллелизм 10 создаёт шум

При `PARALLEL_TASKS=10` десять задач бьются в один cloud-endpoint kimi одновременно. Это:
- Увеличивает разброс latency на каждом шаге (rate limit, queueing)
- Меняет порядок завершения задач → разный вклад классификатора-кеша
- Может провоцировать retry-логику в Ollama-бэкенде (skip silently в json_extract)

Это объясняет, почему на одной и той же конфигурации скор плавает.

#### H3: Reasoning-ошибки модели

Четыре из шести провалов — ошибки рассуждения kimi-k2:
- **t41** — `31-03-2026` vs правильная дата: ошибка date arithmetic даже при наличии code_eval
- **t39** — забыл приложить `contacts/mgr_001.json` в grounding_refs
- **t30/t42** — при `unreadable file` лезет в `OUTCOME_NONE_CLARIFICATION` вместо `find`/`search` альтернативы

Эти ошибки воспроизводятся и с включённым evaluator **только частично** — bypass-матрица пропускает их (см. 10.3).

#### H4: Bypass-матрица обесценивает evaluator даже при включении

Даже если пользователь поставит `EVALUATOR_ENABLED=1`, текущая логика в `loop.py:1917–1949` обнуляет пользу:
- `TASK_LOOKUP` → всегда bypass (3 провала: t30/t39/t42 — все lookup)
- OTP+no-intercept → bypass (t29)
- 0 search results → bypass

Значит включение EVALUATOR_ENABLED=1 даст лишь частичный выигрыш (спасёт t10) без правки bypass.

#### H5: Низкая воспроизводимость

Параллелизм + недетерминизм kimi + set-order в `prephase.py` + двойная классификация FIX-265c → разброс 45→88% **между прогонами одной конфигурации**.

**Что ОПРОВЕРГНУТО по сравнению с первым черновиком:**
- ❌ «TASK_TIMEOUT_S=180s → массовые таймауты» — в реальности `TASK_TIMEOUT_S=900s` из .env, 0 таймаутов
- ❌ «Evaluator отклоняет задачи и это проблема» — evaluator **намеренно выключен**, не вызывался
- ❌ «Prompt_builder тихо сломан (fail-open)» — builder **намеренно выключен** флагом `PROMPT_BUILDER_ENABLED=0`
- ❌ «×10 output tokens kimi бьют по лимиту» — в budget'е при 900s таймауте
- ❌ «DSPy addendum конфликтует с _CORE» — addendum не генерируется в принципе

---

## 11. Рекомендации (пересмотрено под реальный .env)

Главный вывод: **базовый harness на 86% уже работает без feedback-сети**. Чтобы перейти обратно к 90%+, нужен **умный возврат feedback-слоёв** (не любой ценой) + **правка bypass** + **фикс reasoning** для конкретных провалов.

| # | Приоритет | Действие | Ожидаемый эффект | Риск |
|---|---|---|---|---|
| 1 | **P0** | **Включить EVALUATOR_ENABLED=1 одновременно с сужением bypass.** Убрать безусловный bypass для TASK_LOOKUP (loop.py:1928) — пусть evaluator хотя бы проверяет наличие grounding_refs и outcome-логику | +2–3 pp (t10, t39 ловятся) | +latency: evaluator — это +1 LLM-вызов на kimi-thinking, до 15–20s на задачу |
| 2 | **P0** | **FIX-279 OTP-bypass** дополнить проверкой тела сообщения (msg.body) на security-keywords перед auto-approve. Без неё t29-подобные threat-в-OTP пропускаются | чинит t29-класс (~2 pp) | минимальный, проверка локальная |
| 3 | **P0** | **Для TASK_LOOKUP** усилить правило в `_CORE`: «при unreadable/error file сначала пробовать find/search/list альтернативы; CLARIFICATION — только после 2+ неудачных попыток» | чинит t30/t42 (~4 pp) | нужно проверить на email-задачах чтобы не увести в зацикливание |
| 4 | **P1** | **Включить PROMPT_BUILDER_ENABLED=1** на A/B прогонах (3 прогона по 43 задачи). Сравнить средний скор с `=0` | если +3pp — оставить; если -2pp — выключить назад; покажет, ломает ли addendum `_CORE` | +1 LLM-вызов на задачу (kimi-thinking ~10s) |
| 5 | **P1** | Добавить метрики в summary: `evaluator_bypass_rate`, `builder_used_rate`, `router_fallback_rate` | делает выключенность feedback-слоёв видимой, а не «тихой» | — |
| 6 | **P1** | **Снизить PARALLEL_TASKS с 10 до 3–5** на отладочных прогонах для изучения деградации; 10 оставить для финальных бенчей | убирает шум параллелизма из диагностики; стабилизирует разброс | прогоны идут дольше |
| 7 | **P1** | Стабилизировать `prephase.py`: `sorted(set(...))` для top-level dirs и mentioned paths | убирает флакинг classifier | — |
| 8 | **P2** | Единая классификация: либо regex-only для HIGH_CONF, либо LLM-only для всех | убирает двоякость выбора модели | возможно потеря точности на ambiguous-задачах |
| 9 | **P2** | Code_eval-шаблон для date arithmetic в `_CORE`: «если задача содержит дату/период → обязательно code_eval, не пытаться считать в уме» | чинит t41-класс | +тoken в prompt |
| 10 | **P2** | Юнит-тесты security gates (FIX-203/206/214/215/250/253/255/259) + тесты bypass-матрицы | защита от регрессий | — |
| 11 | **P2** | Использовать `DSPY_COLLECT=1` → накопить ≥30 примеров → `optimize_prompts.py --target all` → применить компиляцию | builder при ENABLED=1 будет сильнее чем сырой DSPy Predict | нужно 3–5 прогонов для накопления |
| 12 | **P3** | Декомпозиция `loop.py`: вынести evaluator/dual-write/cross-account в отдельные модули | ускоряет итерацию | разовая стоимость рефакторинга |
| 13 | **P3** | Full-trace replay (request/response JSONL) для офлайн-отладки | дешевле диагностика провалов | — |

### Порядок действий для возврата к 90%+

```mermaid
flowchart LR
    A[текущие 86%<br/>feedback OFF] --> B[P0: сузить bypass<br/>+ EVALUATOR_ENABLED=1]
    B --> C[+2-3pp<br/>~88%]
    C --> D[P0: fix lookup read-error rule]
    D --> E[+3-4pp<br/>~90%+]
    E --> F[P1: A/B prompt_builder<br/>+ снизить PARALLEL_TASKS для стабильности]
    F --> G[стабильно 90-92%]
```

**Не делать:**
- Не менять модель (kimi-k2.5 подтверждено работает на 86% без помощи)
- Не включать evaluator без правки bypass — получите +latency без +скора
- Не увеличивать PROMPT_BUILDER_MAX_TOKENS выше 1000 — это уже избыточно для kimi-thinking

---

## 12. Итоговая оценка (AI harness, после полной сверки с .env)

| Ось | Оценка | Δ vs 2026-04-08 | Подтверждение фактами |
|---|---|---|---|
| Feedforward | A | = | classifier + base prompt держат 86% в одиночку |
| **Feedback** | **C+** | **↓** | evaluator выключен флагом; bypass-матрица обесценила бы его даже при включении |
| Мульти-модель | A | = | все 8 типов роутятся, thinking-модели подключены |
| Песочница | B+ | = | SIGALRM, protected paths не менялись |
| Наблюдаемость | C+ | ↓ | нет метрик bypass/builder/fallback — выключения незаметны |
| Тестирование | D | = | 4 файла, 0 покрытие security gates и bypass-матрицы |
| Мокирование | F | = | — |
| Управление циклом | B | ↑ | `TASK_TIMEOUT_S=900` в .env + 0 таймаутов за прогон |
| Модульность | C | ↑ | loop.py 2604→2190, stall/security/json_extract вынесены |
| Обработка ошибок | C | = | fail-open скрывает выключенный builder, если его пытались использовать |
| Воспроизводимость | D | = | разброс 45–88%, усиливается `PARALLEL_TASKS=10` |
| Адаптивный бюджет | C- | ↑ | `TASK_TIMEOUT_S=900` даёт запас для thinking-моделей |

**Итого: B-** — harness достигает 86% **без** двух главных feedback-слоёв (evaluator и prompt_builder намеренно выключены). Это значит базовая связка `classifier + prompt + security gates + stall` здорова. Путь к 90%+ — **не «починка инфраструктуры»**, а **аккуратный возврат feedback-слоёв с правкой bypass-матрицы** + **три таргетированных фикса reasoning** (lookup read-error, OTP body-check, date arithmetic). Не менять модель, не крутить таймаут, не ломать рабочую базу.

---

## 13. DSPy-усиление: варианты и per-domain стратегия

### 13.1 Текущее состояние DSPy-интеграции

```
data/
  dspy_synthetic.jsonl        — 25 cold-start примеров (вручную, по типам)
  dspy_examples.jsonl         — отсутствует (PROMPT_BUILDER_ENABLED=0 → collect не срабатывает)
  prompt_builder_program.json — отсутствует (COPRO ни разу не запускался)
  evaluator_program.json      — отсутствует

agent/
  prompt_builder.py   — dspy.Predict(PromptAddendum) — один Signature для всех 8 типов
  evaluator.py        — dspy.ChainOfThought(EvaluateCompletion) — один Signature
  dspy_examples.py    — сборщик примеров, общий JSONL без разбивки по доменам
  dspy_lm.py          — DispatchLM-адаптер (Anthropic/OpenRouter/Ollama)
  optimize_prompts.py — COPRO-раннер, один program per компонент
```

**Синтетические примеры по доменам (dspy_synthetic.jsonl, 25 шт.):**

| Домен | Кол-во | Описание задач |
|---|---|---|
| lookup | 5 | поиск контактов, подсчёты |
| default | 4 | создание/чтение файлов |
| distill | 3 | агрегация, суммаризация |
| email | 3 | отправка email через outbox |
| inbox | 3 | обработка входящих |
| longContext | 3 | батч-операции |
| coder | 2 | code_eval задачи |
| think | 2 | анализ, сравнение |

**Ключевые ограничения текущей архитектуры:**
1. `PromptAddendum` содержит 70+ строк правил для ВСЕХ доменов в одном docstring — COPRO ищет **одну** инструкцию-компромисс
2. `get_trainset()` возвращает примеры всех типов без фильтрации — cross-domain шум в обучении
3. При разных долях доменов в trainset'е (lookup:5, think:2) COPRO смещается к большинству
4. Один скомпилированный `program.json` — нельзя откатить только email без откатки всего
5. Сбор примеров не работает пока `PROMPT_BUILDER_ENABLED=0` → цикл оптимизации заморожен

### 13.2 Архитектура: Monolithic vs Per-Domain

```mermaid
graph TD
    subgraph Monolithic["Текущая: Monolithic"]
        T1[task_type=email] --> PB1[PromptAddendum<br/>70+ строк всех правил]
        T2[task_type=lookup] --> PB1
        T3[task_type=inbox] --> PB1
        T4[...остальные 5] --> PB1
        PB1 --> PROG1[(prompt_builder_program.json<br/>один для всех)]
        COPRO1[COPRO] --> PROG1
        DS1[(dspy_examples.jsonl<br/>все типы вперемешку)] --> COPRO1
    end

    subgraph PerDomain["Целевая: Per-Domain"]
        TE[task_type=email] --> PBE[PromptAddendumEmail<br/>~15 строк]
        TL[task_type=lookup] --> PBL[PromptAddendumLookup<br/>~12 строк]
        TI[task_type=inbox] --> PBI[PromptAddendumInbox<br/>~15 строк]
        TO[...остальные 5] --> PBO[PromptAddendum{Type}<br/>...]

        PBE --> PROGE[(program_email.json)]
        PBL --> PROGL[(program_lookup.json)]
        PBI --> PROGI[(program_inbox.json)]
        PBO --> PROGO[(program_{type}.json)]

        DSE[(dspy_examples_email.jsonl)] --> COPRO_E[COPRO email]
        DSL[(dspy_examples_lookup.jsonl)] --> COPRO_L[COPRO lookup]
        DSI[(dspy_examples_inbox.jsonl)] --> COPRO_I[COPRO inbox]
        COPRO_E --> PROGE
        COPRO_L --> PROGL
        COPRO_I --> PROGI
    end
```

### 13.3 Варианты DSPy-усиления (V1–V7)

#### V1 — Per-domain PromptAddendum (основной, рекомендован) ★★★

**Что:** Разбить один `PromptAddendum` на 8 специализированных Signature, каждый со своим docstring, своим JSONL и своим `program_{type}.json`.

**Архитектура сбора и загрузки:**

```mermaid
sequenceDiagram
    participant M as main.py
    participant A as run_agent
    participant C as classifier
    participant PB as prompt_builder
    participant DS as dspy_examples

    M->>A: task_text
    A->>C: classify → task_type="email"
    A->>PB: build_dynamic_addendum(type="email")
    PB->>PB: load program_email.json (если есть)
    PB->>PB: dspy.Predict(PromptAddendumEmail)
    PB-->>A: addendum

    note over M: по завершению задачи
    M->>DS: record_example(..., task_type="email", score=1.0)
    DS->>DS: append → dspy_examples_email.jsonl
    DS->>DS: count? ≥ THRESHOLD_EMAIL? → hint
```

**Изменения кода:**
- `agent/prompt_builder.py`: один класс-фабрика `get_signature(task_type)` → возвращает нужный подкласс Signature
- `agent/dspy_examples.py`: `_path_for(task_type)` → `data/dspy_examples_{type}.jsonl`
- `optimize_prompts.py`: новый `--target builder-email`, `--target builder-lookup`, etc. + `--target builder-all`
- Программы: `data/prompt_builder_{type}_program.json`

**Trade-offs:**

| Плюс | Минус |
|---|---|
| Каждый Signature фокусирован (15 строк вместо 70+) | 8× больше файлов program.json |
| COPRO оптимизирует без cross-domain шума | Нужно ≥ THRESHOLD примеров per тип |
| Откат/обновление одного домена без эффекта на другие | Холодный старт для редких типов (coder, distill) |
| Видно, какой домен улучшается при COPRO | Код чуть сложнее |

**Порог для per-domain сбора:**
```
email:    THRESHOLD=15  (≥3 задач в каждом прогоне)
inbox:    THRESHOLD=15
lookup:   THRESHOLD=12
default:  THRESHOLD=12
distill:  THRESHOLD=10
longCtx:  THRESHOLD=10
think:    THRESHOLD=8   (редкий)
coder:    THRESHOLD=8   (редкий)
```

---

#### V2 — Bootstrap Few-Shot per domain (холодный старт) ★★★

**Что:** Вместо (или дополнительно к) COPRO использовать `BootstrapFewShotWithRandomSearch` для доменов с малым trainset'ом. Метод выбирает лучшие demo-примеры из синтетических, не оптимизируя инструкцию.

**Когда применять:** <15 реальных примеров на домен (coder, think, distill).

```mermaid
flowchart LR
    SYNT[(dspy_synthetic_{type}.jsonl<br/>2-5 примеров)] --> BOOT[BootstrapFewShotWithRandomSearch<br/>num_candidate_sets=8, max_bootstrapped_demos=3]
    REAL[(dspy_examples_{type}.jsonl<br/>≥15 примеров)] --> COPRO[COPRO<br/>breadth=8, depth=4]
    BOOT --> PROG_BOOT[(program_{type}_boot.json)]
    COPRO --> PROG_COPRO[(program_{type}_copro.json)]
    PROG_BOOT --> LOAD{≥15 реальных?}
    LOAD -->|нет| USE_BOOT[использовать boot]
    LOAD -->|да| USE_COPRO[использовать copro]
    PROG_COPRO --> USE_COPRO
```

---

#### V3 — DSPy-классификатор (замена LLM-вызова в classifier.py) ★★

**Что:** Текущий `classify_task_llm()` — сырой промт через DispatchLM. Заменить на `dspy.Predict(ClassifyTask)` с COPRO-оптимизацией.

**Почему ценно:** Классификатор вызывается на каждой задаче. Ошибка классификации → неправильная модель → cascade failure. Сейчас нет ни метрик точности, ни оптимизации.

```python
class ClassifyTask(dspy.Signature):
    """Classify a PAC1 task into one of 8 types: ..."""
    task_text: str = dspy.InputField()
    agents_md: str = dspy.InputField()
    task_type: str = dspy.OutputField()  # одно из 8 значений
```

**Сбор примеров:** уже есть в каждом прогоне — `task_text → task_type` + `score` как прокси точности. Ground truth: если задача прошла с первого раза (score=1.0), классификация была верной.

**Метрика COPRO:** `predicted_type == ground_truth_type` (или proxy: score задачи с данной классификацией ≥ 0.8).

---

#### V4 — Per-domain evaluator (разделить EvaluateCompletion) ★★

**Что:** Текущий `EvaluateCompletion` — один ChainOfThought для всех типов. Inbox-задачи требуют проверки security (cross-account, sender domain), lookup — только grounding_refs, email — outbox + адрес получателя.

**Архитектура:**

```mermaid
flowchart TD
    EVAL_CALL[evaluate_completion<br/>task_type=?] --> ROUTE{task_type}
    ROUTE -->|inbox| EI[EvaluateCompletionInbox<br/>+security check<br/>+sender domain match]
    ROUTE -->|email| EE[EvaluateCompletionEmail<br/>+outbox written<br/>+recipient match]
    ROUTE -->|lookup| EL[EvaluateCompletionLookup<br/>+grounding_refs present<br/>+answer format]
    ROUTE -->|default/distill/think| ED[EvaluateCompletionDefault<br/>+done_ops completeness]
    EI --> VERDICT[approved / rejected + hint]
    EE --> VERDICT
    EL --> VERDICT
    ED --> VERDICT
```

**Важно:** Per-domain evaluator + пересмотр bypass-матрицы (раздел 11, P1) дают синергию: убрать безусловный bypass для TASK_LOOKUP и использовать `EvaluateCompletionLookup` с фокусом только на grounding_refs.

---

#### V5 — DSPy Assertions для hard-constraints ★★

**Что:** Добавить `dspy.Assert` и `dspy.Suggest` к Signature'ам builder и evaluator. Это DSPy-механизм принудительной проверки выхода с авто-ретраем при нарушении.

```python
# В build_dynamic_addendum — после predict:
dspy.Assert(
    len([l for l in result.addendum.splitlines() if l.startswith("-")]) >= 2,
    "Addendum must have at least 2 bullet points"
)
dspy.Suggest(
    "code_eval" in result.addendum if task_type in ("lookup", "think") else True,
    "Lookup/think tasks should suggest code_eval for bulk scanning"
)
```

**Эффект:** устраняет t41-class (think без code_eval) и t39-class (lookup без grounding_ref) на уровне builder — до того, как агент начнёт работать.

---

#### V6 — DSPy ReAct для evaluator (верификация через чтение файлов) ★

**Что:** Текущий evaluator видит только `done_ops` (список строк) — он не может проверить содержимое написанного файла. `dspy.ReAct` с `read_file` инструментом дал бы evaluator'у возможность верифицировать факты.

**Риск:** +1 LLM-агент внутри шага loop → значительная latency. Применимо только при `EVAL_EFFICIENCY=high` и большом запасе до таймаута.

---

#### V7 — MIPROv2 вместо COPRO (когда накопится ≥ 50 примеров на домен) ★

**Что:** COPRO оптимизирует только instruction (docstring). MIPROv2 оптимизирует **и** instruction, **и** few-shot demo selection. При достаточном trainset'е даёт +5–15% accuracy по сравнению с COPRO.

**Условие:** `≥50 реальных примеров на домен`. До этого — COPRO + Bootstrap.

---

### 13.4 Mermaid: полный per-domain DSPy pipeline

```mermaid
flowchart TB
    subgraph Collect["1. Сбор (каждый прогон)"]
        RUN[main.py прогон] -->|task завершена, score=S| REC
        REC[record_example<br/>task_type, addendum, score, vault_tree, agents_md]
        REC -->|email| DE[(dspy_examples_email.jsonl)]
        REC -->|lookup| DL[(dspy_examples_lookup.jsonl)]
        REC -->|inbox| DI[(dspy_examples_inbox.jsonl)]
        REC -->|...| DO[(dspy_examples_{type}.jsonl)]
    end

    subgraph Optimize["2. Оптимизация (по достижении порога)"]
        DE -->|≥15| CE[COPRO PromptAddendumEmail]
        DL -->|≥12| CL[COPRO PromptAddendumLookup]
        DI -->|≥15| CI[COPRO PromptAddendumInbox]
        DO -->|≥10| CO[COPRO PromptAddendum{Type}]
        SYNT[(dspy_synthetic_{type}.jsonl)] -->|<порог| BFS[BootstrapFewShot]
        CE --> PE[(program_builder_email.json)]
        CL --> PL[(program_builder_lookup.json)]
        CI --> PII[(program_builder_inbox.json)]
        CO --> PO[(program_builder_{type}.json)]
        BFS --> PB[(program_builder_{type}_boot.json)]
    end

    subgraph Inference["3. Инференс (build_dynamic_addendum)"]
        TYPE[task_type] --> FACT[get_signature(task_type)<br/>→ PromptAddendum{Type}]
        FACT --> LOAD[load program_{type}.json<br/>или _boot.json<br/>или базовый]
        LOAD --> PRED[dspy.Predict + context(lm)]
        PRED --> ADD[addendum 3–6 bullets]
    end

    Collect --> Optimize
    Optimize --> Inference
```

### 13.5 Матрица вариантов по приоритету

| # | Вариант | Сложность | Ожидаемый прирост | Зависимости |
|---|---|---|---|---|
| V1 | Per-domain PromptAddendum + JSONL | Средняя | **+3–5 pp** (фокус инструкций) | PROMPT_BUILDER_ENABLED=1 |
| V2 | Bootstrap Few-Shot для редких доменов | Низкая | **+1–2 pp** (coder/think/distill) | V1 |
| V3 | DSPy-классификатор | Средняя | **+1–3 pp** (стабильность классификации) | независимо |
| V4 | Per-domain evaluator | Средняя | **+2–4 pp** + bypass fix | EVALUATOR_ENABLED=1 |
| V5 | DSPy Assertions в builder | Низкая | **+1–2 pp** (t41/t39 класс) | V1 |
| V6 | ReAct evaluator | Высокая | +1 pp | V4, большой таймаут |
| V7 | MIPROv2 | Средняя | +5% vs COPRO | ≥50 примеров/домен |

### 13.6 Поэтапный план реализации

```mermaid
gantt
    title Миграция на per-domain DSPy
    dateFormat  YYYY-MM-DD
    section Этап 1 — Фундамент
    per-domain JSONL collector  :a1, 2026-04-19, 2d
    per-domain Signature фабрика :a2, after a1, 2d
    synthetic cold-start per-domain :a3, after a1, 1d
    PROMPT_BUILDER_ENABLED=1 + прогон сбора :a4, after a2, 1d

    section Этап 2 — Оптимизация builder
    optimize_prompts per-domain CLI :b1, after a4, 2d
    COPRO запуск email+inbox+lookup :b2, after b1, 1d
    Bootstrap для coder/think/distill :b3, after b1, 1d

    section Этап 3 — Evaluator
    bypass fix TASK_LOOKUP + FIX-279 :c1, after b2, 1d
    EVALUATOR_ENABLED=1 + прогон :c2, after c1, 1d
    per-domain EvaluateCompletion :c3, after c2, 3d

    section Этап 4 — Укрепление
    DSPy Assertions в builder :d1, after c3, 1d
    DSPy-классификатор :d2, after c3, 2d
    MIPROv2 при ≥50 примеров :d3, after d2, 2d
```

**Этап 1** (4 дня) даёт возможность собирать данные. Без этого всё остальное невозможно.

**Этап 2** (3 дня) даёт первые оптимизированные программы — ожидаемый эффект **+3–5 pp** при включении builder.

**Этапы 3–4** добавляют feedback-слой и hardening — ещё **+2–4 pp**.

**Суммарно: к 90–93%** при полной реализации и достаточном trainset'е.

---

*Источники: исходники `agent/`, логи в `logs/`, `docs/harness-analysis.md` (2026-04-08), `docs/evaluator.md`, git log последних 2 недель, реальный `.env` прогона `20260418_130556_kimi-k2.5-cloud`.*
