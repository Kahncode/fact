# Backlog

Ideas and problems that are not yet tasks. Planning turns them into task files (or, after M2, issues).

## Human actions

Some steps can only be done by a person: downloading the GitHub App private key, installing the App on a repository, installing the Temporal CLI, logging Claude Code in, approving a permission. Tasks list them in `human_actions`, but fact has no way yet to ask for them and track them.

Needs a design for:
- how a workflow or task declares a human action, and blocks on it;
- how the person is asked (inbox, notification);
- how they confirm it is done, and how fact checks it where it can (for example, the key file exists and a token can be minted);
- reminders when a request waits too long.

It probably builds on the gate and notification work in M2 (M2-01 to M2-03).

## Later milestones (from the spec)

- M3 UI: inbox, runs, run detail with stage graph, live logs, notifications.
- M4: classification and auto-merge, `@fact` comment trigger, multi-repo change sets.
- M5 Planner: the planning phase as a workflow, and the MCP server for planning from Claude. Until then, Claude Code in this repository is the planner (`plan-tasks` skill).
- M6 Hardening: Docker runner, Codex harness, budgets that pause runs, Costs view.
- Linear task source: in the spec, not yet scheduled.

## Ideas

- Evaluate Temporal's Claude Agent SDK plugin against the subprocess harness (the spec says during M1; it's not blocking, so it lives here until someone needs it).
- Validate `.fact/` changes on the PR that makes them.
