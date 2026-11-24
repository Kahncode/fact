# Reference workflow: issue to PRs

```mermaid
flowchart LR
    I[implement] --> C{lint + test}
    C -- fail, max 3 --> F[fix_checks] --> C
    C -- pass --> R[review]
    R -- findings --> AR[address_review] --> C2{lint + test}
    R -- clean --> U[update_context]
    C2 --> U
    U --> CL[classify] --> P[open PRs] --> CI{CI}
    CI -- fail, max 2 --> FC[fix_ci] --> CI
    CI -- green --> M[wait for merge]
    M -- "@fact comment" --> AC[address_comment] --> CI
```

The standard workflow is code, lint and test, agent review, classify, then PRs. Every failure before the PR, every CI failure and every `@fact` comment starts a new agent session in the run's sandbox. Merging needs no agent: it is automatic or yours, depending on the classification. It starts once the Planning phase has produced an approved plan; incident workflows can add a preview deploy on the same base.
