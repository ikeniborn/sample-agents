# cc-agent — Multi-Agent Pipeline Data Flow (MULTI_AGENT=1)

```mermaid
sequenceDiagram
    participant R as runner.py
    participant API as BitGN API
    participant CLS as Classifier
    participant EXE as Executor
    participant VER as Verifier
    participant MCP as mcp_pcm.py
    participant VAULT as PCM Vault

    R->>API: start_playground(task_id)
    API-->>R: harness_url, instruction, trial_id

    Note over R,CLS: Phase 1 - Classifier (MCP_MODE=readonly, model=$CLAUDE_CLASSIFIER_MODEL, cwd=/tmp)

    R->>CLS: system_prompt=CLASSIFIER_PROMPT<br/>user_prompt=instruction<br/>output_format=json

    CLS->>MCP: initialize (vault context injected into instructions)
    MCP->>VAULT: context()
    VAULT-->>MCP: vault_today, metadata
    MCP-->>CLS: inject vault_today into session

    CLS->>MCP: get_context() / read() / tree() / search()
    Note right of CLS: Reads: AGENTS.md, README.md,<br/>CLAUDE.md, soul.md,<br/>rem_001.json, contacts/
    MCP->>VAULT: read vault files
    VAULT-->>MCP: file content
    MCP-->>CLS: vault data

    CLS-->>R: classification.json<br/>{schema_version, task_type,<br/>vault_structure, key_rules[],<br/>trust_tiers, compliance_flags,<br/>system_prompt, warnings[]}

    Note over R: parse_classifier_output()<br/>build_executor_prompt():<br/>system_prompt + vault_ctx<br/>+ key_rules + warnings

    Note over R,EXE: Phase 2 - Executor (MCP_MODE=draft, model=$CLAUDE_MODEL, cwd=/tmp)

    R->>EXE: system_prompt=built_executor_prompt<br/>user_prompt=instruction<br/>MCP env: DRAFT_FILE=draft_N.json

    EXE->>MCP: tool calls (read, write, delete, mkdir, move)
    Note right of MCP: Layer 1: write/delete guards<br/>Layer 2: injection detection<br/>Layer 3: stall detection<br/>Layer 4: evaluator gate
    MCP->>VAULT: buffer mutations in memory
    EXE->>MCP: report_completion(outcome, message, refs)
    MCP->>VAULT: replay buffered writes (if ok) or discard
    MCP-->>EXE: writes draft_N.json to DRAFT_FILE

    EXE-->>R: exit (draft_N.json on disk)<br/>{schema_version, outcome,<br/>message, refs[]}

    Note over R,VER: Phase 3 - Verifier (MCP_MODE=readonly, model=$CLAUDE_VERIFIER_MODEL, cwd=/tmp)

    R->>VER: system_prompt=VERIFIER_PROMPT<br/>user_prompt={instruction, draft_answer}

    VER->>MCP: get_context() / read() / search()
    Note right of VER: Reads: AGENTS.md, CLAUDE.md,<br/>rem_001.json (original due_on),<br/>refs from draft to verify
    MCP->>VAULT: read
    VAULT-->>MCP: content
    MCP-->>VER: vault data

    VER-->>R: verdict_N.json<br/>{schema_version, vault_date,<br/>verdict: approve|correct|reject,<br/>outcome, message, refs[], reason}

    alt verdict == reject AND attempt <= $MAX_RETRIES AND time_remaining > 30s
        Note over R: feedback_prompt =<br/>executor_prompt +<br/>"## Feedback from verifier (attempt N)\n" +<br/>verdict.reason +<br/>"Fix the issues above and try again."
        R->>EXE: retry with feedback_prompt (attempt N+1)
    end

    Note over R: apply_verdict(draft, verdict)<br/>approve: merge refs<br/>correct/reject: use verifier outcome+message+refs

    R->>VAULT: _submit_answer() via vm.answer()<br/>{outcome, message, refs[]}
    R->>API: end_trial(trial_id)
    API-->>R: score, score_detail
```

## Параметры запуска агентов

| Параметр | Classifier | Executor | Verifier |
|----------|-----------|----------|----------|
| `MCP_MODE` | `readonly` | `draft` | `readonly` |
| `model` | `$CLAUDE_CLASSIFIER_MODEL` (default: haiku) | `$CLAUDE_MODEL` | `$CLAUDE_VERIFIER_MODEL` (auto: отличается от executor) |
| `cwd` | `/tmp` | `/tmp` | `/tmp` |
| `output_format` | `json` (envelope unwrap) | — | `json` (envelope unwrap) |
| `timeout` | `$CLASSIFIER_TIMEOUT_S` (60s) | динамически из бюджета | `$VERIFIER_TIMEOUT_S` (90s) |

> **cwd=/tmp**: все агенты запускаются из нейтрального каталога, чтобы Claude Code не поднимался вверх по дереву и не подхватил CLAUDE.md репозитория. Это не `--bare` флаг (он требует ключ) — именно переопределение рабочей директории.

> **Retry**: повтор executor при `verdict=reject` управляется `$MAX_RETRIES` (default: 1) и жёстким порогом `time_remaining > 30s` (hardcoded в `_executor_verify_loop`).

## Схемы данных обмена между агентами

### Classifier → runner.py (`classification.json`)

```json
{
  "schema_version": 1,
  "task_type": "inbox|email|lookup|delete|capture|other",
  "vault_structure": "Personal CRM: accounts/, contacts/, ...",
  "key_rules": ["exact rule quoted from AGENTS.md"],
  "trust_tiers": {},
  "compliance_flags": {"acct_001": ["nda_signed"]},
  "system_prompt": "You are a CRM executor. Vault root is /. Steps: ...",
  "warnings": ["external_send_guard on acct_004 — informational"]
}
```

### runner.py → Executor (system_prompt)

```
{classification.system_prompt}

## Vault context
{classification.vault_structure}

## Key rules for this task
- {classification.key_rules[0]}
- ...

## Warnings
- {classification.warnings[0]}
- ...
```

При retry добавляется:
```
## Feedback from verifier (attempt N)
{verdict.reason}
Fix the issues above and try again.
```

### Executor → runner.py (`draft_N.json`, через report_completion в MCP)

```json
{
  "schema_version": 1,
  "outcome": "ok|clarification|security|unsupported",
  "message": "Email queued for Luuk Vermeulen",
  "refs": ["/outbox/42.json", "/outbox/seq.json"]
}
```

### runner.py → Verifier (user_prompt)

```json
{
  "instruction": "<original task instruction>",
  "draft_answer": {
    "schema_version": 1,
    "outcome": "ok",
    "message": "...",
    "refs": [...]
  }
}
```

### Verifier → runner.py (`verdict_N.json`)

```json
{
  "schema_version": 1,
  "vault_date": "2026-03-17",
  "verdict": "approve|correct|reject",
  "outcome": "ok|clarification|security|unsupported",
  "message": "corrected message if verdict=correct/reject",
  "refs": ["/outbox/42.json", "/accounts/acct_004.json"],
  "reason": "VAULT DATE: 2026-03-17. Executor used system clock..."
}
```

## Harness Layers in mcp_pcm.py

```mermaid
flowchart LR
    TOOL_CALL["Tool call from Claude"] --> G1

    subgraph harness ["mcp_pcm.py Harness Layers"]
        G1["Layer 1: Write/Delete Guards<br/>(protected paths, inbox/)"]
        G2["Layer 2: Injection Detection<br/>(content scan on read)"]
        G3["Layer 3: Stall Detection<br/>(repeated calls / mutation drought)"]
        G4["Layer 4: Evaluator Gate<br/>(heuristic check before report_completion)"]
    end

    G1 -->|blocked| BLOCK["Return BLOCKED error"]
    G1 -->|pass| G2
    G2 -->|injection| WARN["Append SECURITY WARNING"]
    G2 -->|pass| G3
    G3 -->|stall| HINT["Append SYSTEM HINT"]
    G3 -->|pass| G4
    G4 -->|warning| LOG["Emit eval_warning event"]
    G4 -->|pass| PCM["PcmRuntimeClientSync<br/>(actual vault operation)"]

    style TOOL_CALL fill:#2E86AB,color:#fff,stroke:#1a5276
    style G1 fill:#5D6D7E,color:#fff,stroke:#2c3e50
    style G2 fill:#5D6D7E,color:#fff,stroke:#2c3e50
    style G3 fill:#5D6D7E,color:#fff,stroke:#2c3e50
    style G4 fill:#5D6D7E,color:#fff,stroke:#2c3e50
    style BLOCK fill:#C0392B,color:#fff,stroke:#922b21
    style WARN fill:#C0392B,color:#fff,stroke:#922b21
    style HINT fill:#E67E22,color:#fff,stroke:#a04000
    style LOG fill:#E67E22,color:#fff,stroke:#a04000
    style PCM fill:#28B463,color:#fff,stroke:#1d8348
```
