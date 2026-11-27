# Architecture

```mermaid
flowchart LR
    CH["Channels: CLI, MCP, web UI"] --> API["Factory API (FastAPI)"]
    TS["Task source: task files, GitHub Issues"] --> API
    API -->|start runs, answer gates| T["Temporal server"]
    T --> CW["Worker: control zone"]
    CW --> SB["Run sandbox: git worktrees"]
    CW -->|push, PRs, comments| GH["GitHub"]
    CW -->|events, logs, time, cost| API
    SB -->|events, logs, time, cost| API
    API --> DB[("Factory DB: SQLite")]
    API --> AS[("Artifact store: local folder")]
```

Channels reach the factory API, which starts runs and answers gates through Temporal. The worker runs each step in the run's workspace and pushes PRs to GitHub, and every step reports its logs, time and cost back to the API, which stores them.

Hosted (M5), the API, the control worker, Temporal and the databases run in the cloud and the run sandbox stays on a runner worker wherever the compute is; the runner reaches the factory only through outbound connections. See `19-cloud.md`.
