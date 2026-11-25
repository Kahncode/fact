# Summary

The factory is a service that turns tracker issues (GitHub Issues first, Linear next) into reviewed GitHub PRs, across one or more repos per project, with Temporal running every workflow durably. v0 runs on one machine; remote hosting and more users come later. Its first project is the factory's own repo.

**Goals for v0**

- Work on any git + GitHub project, including projects that span several repos, and including the factory's own repo.
- Workflows are code that combines agents, deterministic tools and persistent data, and can be changed often without breaking runs in flight.
- Every step is inspectable: logs, agent transcript, wall time, human wait time, tokens and cost.
- Humans review in GitHub PRs. The factory UI follows progress, sends you to the PR, and notifies you when something needs you.
- A planning loop turns ideas into specs and tracker issues, usable from Claude now and from the web UI or Slack later.

**Not in v0**: cloud hosting, multiple users and auth, remote sandboxes, Slack, LLM-as-judge evals.
