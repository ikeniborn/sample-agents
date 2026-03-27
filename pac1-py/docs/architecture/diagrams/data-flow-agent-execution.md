# pac1-py — Agent Execution Data Flow

Generated: 2026-03-26

```mermaid
sequenceDiagram
    participant Runner as main.py
    participant Harness as BitGN Harness API
    participant Agent as agent/__init__.py
    participant Router as classifier.py
    participant Pre as prephase.py
    participant PCM as bitgn/vm (PCM runtime)
    participant Loop as loop.py
    participant LLM as dispatch.py

    Runner->>Harness: GetBenchmark(benchmark_id)
    Harness-->>Runner: tasks[]

    loop For each task
        Runner->>Harness: StartPlayground(task_id)
        Harness-->>Runner: trial (harness_url, instruction)
        Runner->>Agent: run_agent(model, harness_url, instruction)

        Agent->>Router: resolve_llm(task_text)
        Router->>LLM: classify task (FIX-75/76)
        LLM-->>Router: think / tool / longContext / default
        Router-->>Agent: (model_id, model_config)

        Agent->>Pre: run_prephase(vm, task_text, system_prompt)
        Pre->>PCM: tree("/", level=2)
        PCM-->>Pre: vault structure
        Pre->>PCM: read("/AGENTS.MD")
        PCM-->>Pre: AGENTS.MD content
        Pre-->>Agent: PrephaseResult (log, preserve_prefix)

        Agent->>Loop: run_loop(vm, model, task_text, pre, cfg)

        Note over Loop,LLM: Up to 30 steps (or TASK_TIMEOUT_S)

        Loop->>Loop: compact_log (prefix + last 5 pairs)
        Loop->>LLM: _call_llm(log, model, cfg)
        Note over LLM: Tier1: Anthropic SDK / Tier2: OpenRouter / Tier3: Ollama (FIX-27 retry)
        LLM-->>Loop: NextStep (state, plan, task_completed, function)
        Loop->>Loop: stall detection FIX-74
        Loop->>PCM: dispatch tool (tree/find/list/read/write/delete/mkdir/move)
        PCM-->>Loop: result

        alt report_completion called
            Loop->>PCM: answer(outcome, message, refs)
        end

        Loop-->>Agent: token_stats
        Agent-->>Runner: token_stats + model_used

        Runner->>Harness: EndTrial(trial_id)
        Harness-->>Runner: score, score_detail
    end

    Runner->>Runner: print summary table
```

## Key Decision Points

| Step | Decision | Fix Label |
|------|----------|-----------|
| Model selection | LLM-based classification (think/tool/longContext/default) | FIX-75 |
| LLM call | 3-tier fallback with 4-attempt retry | FIX-27 |
| JSON parse | Auto-wrap bare function object | FIX-W1 |
| JSON parse | Strip bare reasoning wrapper | FIX-W2 |
| JSON parse | Truncate plan array to max 5 | FIX-W3 |
| JSON parse | Inject missing task_completed field | FIX-77 |
| Stall detection | Repeated action (3x) / error (2x) / no-write (6 steps) | FIX-74 |
| Delete safety | Auto-list parent before delete | FIX-63 |
| Delete safety | Wildcard delete rejection | FIX-W4 |
| Read error | Auto-relist parent after NOT_FOUND | FIX-73 |
| Delete error | Auto-relist parent after NOT_FOUND | FIX-71 |
