# cc-agent Architecture Documentation

## Overview

**cc-agent** is a replacement for the pac1-py agent. Instead of a custom Python LLM loop, it uses Claude Code CLI directly via MCP (Model Context Protocol). The agent executes pac1 benchmark tasks using a three-stage pipeline.

**Core files (4):** `runner.py`, `agents.py`, `mcp_pcm.py`, `prompt.py`

**Architectural pattern:** Multi-agent pipeline (Classifier → Executor → Verifier)

---

## Documents

| File | Description |
|------|-------------|
| [overview.yaml](overview.yaml) | Full architecture spec: components, data flows, configuration, log artifacts |
| [diagrams/dependency-graph.md](diagrams/dependency-graph.md) | Component dependency graph (Mermaid) |
| [diagrams/data-flow-multi-agent.md](diagrams/data-flow-multi-agent.md) | Multi-agent pipeline sequence + harness layers (Mermaid) |

---

## Quick Component Map

```
runner.py          Orchestrator: BitGN API → spawn iclaude → score
  ├─ agents.py     Multi-agent prompts, parsers, protocol (Classifier/Executor/Verifier)
  │    └─ prompt.py  Adaptive prompt: classify task → base prompt + addendum
  └─ mcp_pcm.py   MCP server + harness: guards, stall detection, evaluator, replay log
```

---

## Pipeline at a Glance

```
BitGN API
  ↓ start_playground / start_trial
runner.py
  ↓ spawn iclaude (readonly MCP)
Classifier (haiku) ── reads vault ──→ classification.json
  ↓ build_executor_prompt()
runner.py
  ↓ spawn iclaude (draft MCP)
Executor (main model) ── performs task ──→ draft_N.json
  ↓ spawn iclaude (readonly MCP)
Verifier (different model) ── verifies ──→ verdict_N.json
  ↓ apply_verdict() / retry on reject
runner.py
  ↓ _submit_answer() → end_trial()
Score
```

---

## MCP Modes

| Mode | Tools | report_completion behavior |
|------|-------|---------------------------|
| `full` | All tools | Calls `vm.answer()` directly |
| `readonly` | tree, find, search, list, read, get_context | N/A (no report_completion) |
| `draft` | All tools | Buffers mutations; commits on `outcome=ok`, discards otherwise |

---

## Harness Layers (mcp_pcm.py)

1. **Write/Delete Guards** — protects AGENTS.MD, docs/channels/, inbox/
2. **Injection Detection** — scans read content for prompt injection patterns
3. **Stall Detection** — warns on repeated identical calls or mutation drought
4. **Evaluator Gate** — heuristic checks before report_completion (AGENTS.MD read, mutations for action tasks)
