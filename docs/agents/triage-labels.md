# Triage labels

The skills speak in five canonical triage roles. fact already has its own labels (`docs/spec/12-integrations.md`), so each role maps onto them rather than onto new labels.

| Role (mattpocock/skills) | On a `Kahncode/fact` issue                                             | Meaning                                                    |
| ------------------------ | ---------------------------------------------------------------------- | ---------------------------------------------------------- |
| `needs-triage`           | `fact:planned`                                                         | Planned, waiting for the person to approve it (a parent issue) |
| `needs-info`             | `fact:planned` + a comment with the open questions                     | Waiting on an answer from the person                       |
| `ready-for-agent`        | `fact:ready`                                                           | The factory's scheduler may start it (a sub-issue also needs every ancestor ready) |
| `ready-for-human`        | no `fact:ready`, assigned to the person (`--add-assignee Kahncode`)    | Needs work that only a person can do                       |
| `wontfix`                | closed as not planned (`gh issue close <n> --reason "not planned"`)     | Will not be actioned                                       |

Rules:

- Until the M5 planner, agents create leaf issues `fact:ready` and parent issues `fact:planned` (`docs/agents/issue-tracker.md`). Only the person sets a parent `fact:ready`.
- `fact:needs-human` belongs to the factory: it means fact will not touch the issue until a person answers. Triage never sets or removes it. `fact:running` is retired.
- Every issue also gets one `type:*` (`feature`, `bug`, `chore`, `incident`, `epic`) and, unless it is an epic, one `complexity:*` (`trivial`, `standard`, `complex`) label. The default complexity is `standard`. A `priority:*` label (`urgent`, `high`, `medium`, `low`) is optional; none means `medium`.
- These labels already exist (`uv run fact github labels sync Kahncode/fact`); don't create new triage labels.
