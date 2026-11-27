# Requirements

Every requirement below comes from Samuel; the right column says where this spec answers it.

| # | Requirement | Answered in |
| --- | --- | --- |
| R1 | A UI to manage the factory directly | UI and notifications |
| R2 | An orchestrator | Decision: Temporal |
| R3 | A way to define and update workflows, which will change constantly | Workflow authoring |
| R4 | A way to define where and how runners execute; local for now | Runners and harnesses |
| R5 | Tasks and bugs come from an issue tracker: GitHub Issues first, Linear too, through a modular task source | Integrations |
| R6 | Works on any git + GitHub project, including existing ones and the factory itself | Domain model, Dogfooding |
| R7 | A project can span several GitHub repos | Domain model |
| R8 | A planning loop or agent: in Claude or the web UI now, Slack later | Planning agent |
| R9 | Humans review in GitHub PRs; the factory UI follows progress and links to the PR | UI and notifications |
| R10 | The UI and factory manage notifications to the user | UI and notifications |
| R11 | Access to all logs, plus time and cost per step | Observability |
| R12 | Visualize a workflow and inspect every step | UI and notifications |
| R13 | FastAPI + SQLite locally; hosted on Railway with Supabase (database, Auth, Storage) from M5 | Storage and stack |
