# Benchmark Results — bitgn/sandbox

## Comparison Table

| Model | Agent | Date | t01 | t02 | t03 | t04 | t05 | t06 | t07 | Final |
|-------|-------|------|-----|-----|-----|-----|-----|-----|-----|-------|
| anthropic/claude-sonnet-4.6 | agent.py (SGR) | 2026-03-20 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | **100.00%** |
| qwen3.5:9b | agent.py (SGR) | 2026-03-21 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | **100.00%** |
| qwen3.5:4b | agent.py (SGR) | 2026-03-22 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | **100.00%** |
| qwen3.5:2b | agent.py (SGR) | 2026-03-22 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | **100.00%** |

## Summary

All models achieve **100.00%** on bitgn/sandbox benchmark with the SGR Micro-Steps agent.

### Key Fixes by Model

| Fix | Description | Target |
|-----|-------------|--------|
| Fix-62 | Auto-correct AGENTS.MD direct keyword answer | qwen3.5:2b |
| Fix-62b | Filter hallucinated refs when Fix-62 triggers | qwen3.5:2b |
| Fix-28b | Use MISSING-AMOUNT keyword in nav-root force-finish | qwen3.5:2b |
| Fix-54–61 | Pre-phase scaffolding (bypass 4b JSON/instruction failures) | qwen3.5:4b |
| Fix-21–27 | Pre-phase MISSING-AMOUNT, redirect, loop fixes | qwen3.5:9b |

### Individual Reports

- [anthropic/claude-sonnet-4.6](./anthropic-claude-sonnet-4.6.md)
- [qwen3.5:9b](./qwen3.5-9b.md)
- [qwen3.5:4b](./qwen3.5-4b.md)
- [qwen3.5:2b](./qwen3.5-2b.md)
