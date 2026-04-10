# cc-agent — Component Dependency Graph

```mermaid
graph TD
    subgraph orchestration ["Orchestration Layer"]
        runner["runner.py<br/>(Orchestrator)"]
    end

    subgraph agent_logic ["Agent Logic Layer"]
        agents["agents.py<br/>(Prompts / Parsers / Protocol)"]
        prompt["prompt.py<br/>(Adaptive Prompt)"]
    end

    subgraph infrastructure ["Infrastructure Layer"]
        mcp_pcm["mcp_pcm.py<br/>(MCP Server + Harness)"]
    end

    subgraph external ["External Dependencies"]
        bitgn_harness["bitgn.harness_connect<br/>(HarnessServiceClientSync)"]
        bitgn_pcm["bitgn.vm.pcm_connect<br/>(PcmRuntimeClientSync)"]
        iclaude["iclaude CLI<br/>(Claude Code subprocess)"]
    end

    runner --> agents
    runner --> prompt
    runner --> mcp_pcm
    runner --> bitgn_harness
    runner --> bitgn_pcm

    agents --> prompt

    mcp_pcm --> bitgn_pcm

    runner -.->|spawns subprocess| iclaude
    iclaude -.->|stdio MCP protocol| mcp_pcm

    style runner fill:#2E86AB,color:#fff,stroke:#1a5276
    style agents fill:#2E86AB,color:#fff,stroke:#1a5276
    style prompt fill:#2E86AB,color:#fff,stroke:#1a5276
    style mcp_pcm fill:#28B463,color:#fff,stroke:#1d8348
    style bitgn_harness fill:#7F8C8D,color:#fff,stroke:#566573
    style bitgn_pcm fill:#7F8C8D,color:#fff,stroke:#566573
    style iclaude fill:#7F8C8D,color:#fff,stroke:#566573
```

## Legend

| Color | Layer |
|-------|-------|
| Blue (#2E86AB) | Orchestration / Agent Logic |
| Green (#28B463) | Infrastructure (MCP/Harness) |
| Gray (#7F8C8D) | External dependencies |

Solid arrows = Python imports.
Dashed arrows = runtime subprocess / IPC communication.
