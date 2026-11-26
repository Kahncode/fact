# Triage labels

The skills speak in five canonical triage roles. fact already has its own states, so each role maps onto them instead of new labels. Today the state lives in task-file front matter; after M2 it lives in GitHub labels (see `docs/spec/12-integrations.md`).

| Role (mattpocock/skills) | Task file today                                                  | GitHub label after M2 | Meaning                                        |
| ------------------------ | ---------------------------------------------------------------- | --------------------- | ---------------------------------------------- |
| `needs-triage`           | `status: planned`                                                | `fact:planned`        | Written, waiting for the person to approve it  |
| `needs-info`             | `status: planned` + `## Open questions` section                  | `fact:needs-human`    | Blocked on an answer from the person           |
| `ready-for-agent`        | `status: ready`                                                  | `fact:ready`          | Approved; an agent or fact may pick it up      |
| `ready-for-human`        | `human_actions: [...]` non-empty                                 | `fact:needs-human`    | Needs something only a person can do           |
| `wontfix`                | delete the file; note it in `tasks/backlog.md` if worth keeping  | `wontfix`             | Will not be actioned                           |

Rules:

- Agents write new tickets as `status: planned` / `fact:planned`. Only the person promotes to `ready` / `fact:ready`.
- Never set `fact:running`; the factory owns it.
- On GitHub, also set one `type:*` (`feature`, `bug`, `chore`, `incident`) and one `complexity:*` (`trivial`, `standard`, `complex`) label, matching the task file's `type` and `complexity` fields.
- Labels are created by `uv run fact github labels sync Kahncode/fact`; don't create labels by hand.
