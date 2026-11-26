# Decisions and open questions

**Decided (24 Nov 2026)**

| Topic | Decision |
| --- | --- |
| Name | The project is called **fact**: `.fact/` folders, `fact.yaml`, `fact:` labels, `@fact` mentions, `fact/` branches |
| Plan approval | A workflow switch; on for new issues and whenever grilling needs your input |
| Where specs live | The system design in `docs/spec/`, kept current (planning updates it as decisions are made); a feature's spec and plan in its issue description, as the latest plan. PRs describe the code as built, which can differ, and one issue may need several commits or PRs |
| Multi-repo | Supported by the design, not needed yet. The first project is fact itself (one repository); the mobile app (React Native front, FastAPI backend) is the likely first two-repository project |
| Notifications | Desktop notifications from the web app while it is open; Slack and Discord later |
| Harness and budget | Claude Code first, its spend read from Claude Code's own usage reporting; budget in USD per day per project. OpenCode, Pi and more models later |
| Triage | Part of planning: the label mapping first, the planner otherwise, with your review when needed |
| Preview deploys | Per-project steps with their own conditions, never left to an agent; designed when a project needs them |
| Warp | Evaluated 24 Nov 2026. Decided: build fact and own the whole stack; borrow ideas. Blockers: Warp's control plane is always hosted (self-hosted workers are Enterprise-only), Claude Code needs an API key or Bedrock (no subscription), and stage transitions are LLM-evaluated prose. Plans are on sale (Start $0 + usage, Scale $250/month + usage), but Claude Code needs Scale |

**Also decided**

- **Hosting**: local-first for the first versions only. Remote hosting (Linux) and more users come later, so nothing in v0 may assume one machine forever.
- **Platforms**: v0 runs on your Windows PC but must run on Windows, macOS and Linux. Tooling is cross-platform Python (no bash or make), paths use `pathlib`, and containers rely on Docker Desktop on Windows and macOS.
- **Repository**: one monorepo, [Kahncode/fact](https://github.com/Kahncode/fact): a uv workspace of Python packages `core/`, `api/`, `worker/`, `sdk/` and `cli/` (namespace `fact.*`, each with its own `tests/`), plus `web/` for the UI, `docs/spec/` for this spec and `tasks/` for the bootstrap tasks.
- **Auto-merge**: none in v0; you merge everything. The review agent and the rules still classify each change set as high or low risk.
- **Concurrency defaults**: 2 execution sandboxes and 1 planner at a time on your PC.
- **Claude Code**: runs headless under your logged-in subscription in v0. From M2, fact reads Claude Code's actual usage and windows, starts new runs only when the window allows, and a step that hits the limit waits for the reset and resumes.
- **Context**: lives in the repositories; every workflow has a learn step after review.
- **Scoring and benchmarks**: needed soon after v0 to measure whether changes to fact itself (prompts, workflows, models) make it better.
- **Spec in the repository:** this spec is copied to docs/spec/ in M0; from then on the repository copy is the source of truth and agents keep it current.
- **Bootstrap tasks:** until fact ingests GitHub Issues, tasks are markdown files in tasks/\<milestone>/, one file per commit with its context update, dependencies in front matter. Claude Code runs them until fact can.
- **Planner:** its own milestone (M5). Until then Claude Code in the repository is the planner, with skills.
- **Missing task files:** a dependency id whose task file is gone counts as done if its number is below the highest remaining one in its milestone, or its milestone is below the highest present (`FileTaskSource`, M1-02). Finished tasks can then be deleted without rewriting `depends_on`. Cost: `validate()` cannot catch typos below that bound; ids above it are errors. `validate()` runs as a CI test on the real `tasks/` folder.
- **Parent issues (26 Nov 2026):** fact runs only `fact:ready` issues; a sub-issue also needs every ancestor `fact:ready`, so a parent's label gates its plan. A parent's open sub-issues are its dependencies; it then runs as a normal task, and a run with nothing to do ends `no_changes` (success, warning notification). `type:epic` parents are closed without a run. Details in `08-planning.md`.
- **`fact:needs-human` (26 Nov 2026):** set by the factory on an issue it will not touch again until a person acts (a gate, a failed or `needs_human` run, a blocker closed without completing). Only a person's answer clears it (`fact gate`, or removing the label); the scheduler skips such issues and `fact run` refuses them. `fact:running` is retired. Details in `12-integrations.md`.
- **Human actions:** fact needs a way to request and track actions only a person can do, such as downloading the GitHub App private key. In the backlog.

**Still open**

- [x] Python version, resolved 24 Nov 2026

  fact targets Python 3.15 (final since 9 Oct 2026). temporalio 1.36.1, claude-agent-sdk 0.2.188 and every core dependency install from binary wheels on it; the template moves to 3.15 too.
