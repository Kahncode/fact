# Triage labels

The skills speak in five canonical triage roles. fact already has its own labels (`docs/spec/12-integrations.md`), so each role maps onto them rather than onto new labels.

| Role (mattpocock/skills) | On a `Kahncode/fact` issue                                             | Meaning                                                    |
| ------------------------ | ---------------------------------------------------------------------- | ---------------------------------------------------------- |
| `needs-triage`           | `fact:planned`                                                         | Planned and specced, waiting for the person to approve it  |
| `needs-info`             | `fact:planned` + a comment with the open questions                     | Waiting on an answer from the person                       |
| `ready-for-agent`        | `fact:ready`                                                           | Approved; the factory's scheduler may start it             |
| `ready-for-human`        | no `fact:ready`, assigned to the person (`--add-assignee Kahncode`)    | Needs work that only a person can do                       |
| `wontfix`                | closed as not planned (`gh issue close <n> --reason "not planned"`)     | Will not be actioned                                       |

Rules:

- Issues that agents plan and spec are labelled `fact:planned`. Only the person promotes them to `fact:ready`; agents never add it.
- `fact:running` and `fact:needs-human` belong to the factory: they mean a run is in progress or waiting on a gate. Triage never sets or removes them.
- Every planned issue also gets one `type:*` (`feature`, `bug`, `chore`, `incident`) and one `complexity:*` (`trivial`, `standard`, `complex`) label. The default complexity is `standard`.
- These labels already exist (`uv run fact github labels sync Kahncode/fact`); don't create new triage labels.
