# Issue tracker: GitHub Issues on `Kahncode/fact`

fact's own work is planned as GitHub issues on `Kahncode/fact` (`.fact/project.yaml` has `task_source: {kind: github}`). The factory's scheduler starts open issues labelled `fact:ready`, so the body and labels you write are what a run reads. Use the `gh` CLI for every operation; it infers the repo from `git remote -v`.

## Writing an issue (a ticket)

- **The spec lives in the issue body**, as the latest plan (`docs/spec/16-decisions.md`, "Where specs live"). Keep it current by editing the body, not by adding comments.
- **Body sections**, in this order: `## Goal`, `## Read first` (exact files and spec sections), `## Do`, `## Out of scope`, `## Acceptance criteria`. The factory reads the acceptance criteria from the `- [ ]` checklist under the `Acceptance criteria` heading, so every item must be checkable.
- **Labels**: `fact:planned`, one `type:*` (`feature`, `bug`, `chore`, `incident`) and one `complexity:*` (`trivial`, `standard`, `complex`). See `docs/agents/triage-labels.md`.
- **Never add `fact:ready`**: approving the issue is the person's decision. Never add `fact:running` or `fact:needs-human`; the factory sets them.
- **Never edit the text between `<!-- fact:context:start -->` and `<!-- fact:context:end -->`**: the factory owns that section.
- **Splitting work**: one concern per issue, small enough for one run and one PR (see "How to split work" in `tasks/README.md`). Bigger work becomes a parent issue with sub-issues.
- **Dependencies**: GitHub blocked-by links. If you can't add them, write a `Depends on #3, #4` line in the body. An issue runs only once everything it depends on is closed as completed.

## Commands

- **Create**: `gh issue create --title "..." --body-file <file> --label fact:planned --label type:feature --label complexity:standard`. Write the body to a file in the scratchpad and pass it with `--body-file`.
- **Read**: `gh issue view <n> --json number,title,body,labels,comments,state,stateReason`.
- **List**: `gh issue list --state open --label <label> --json number,title,labels`.
- **Sub-issue**: `gh issue create --parent <parent> ...`, or `gh issue edit <parent> --add-sub-issue <child>`.
- **Blocked-by**: `gh api --method POST repos/Kahncode/fact/issues/<n>/dependencies/blocked_by -F issue_id=<blocker database id>`. Get the database id with `gh api repos/Kahncode/fact/issues/<blocker> --jq .id` (not the `#number`).
- **Comment**: `gh issue comment <n> --body-file <file>`.
- **Labels**: `gh issue edit <n> --add-label ... --remove-label ...`.
- **Close**: `gh issue close <n> --reason "not planned" --comment "..."`. Close as completed only for work that is really done: the factory closes its own issues when their PRs merge, and a completed close satisfies dependencies.

## Pull requests as a triage surface

**PRs as a request surface: no.** PRs are opened by the factory (`fact[bot]`) or by you.

## When a skill says "publish to the issue tracker"

Create a GitHub issue as described in **Writing an issue**.

## When a skill says "fetch the relevant ticket"

`gh issue view <n> --json number,title,body,labels,comments`. Run keys look like `gh:Kahncode/fact#12`; the number is the part after `#`.

## Ideas not ready to plan

Add them to `tasks/backlog.md` as a `##` section rather than opening an issue.

## Wayfinding operations

Used by `/wayfinder`. The **map** is one issue and each **child** is a sub-issue. Wayfinding issues never get `fact:*` labels, so the scheduler never runs them. The `wayfinder:*` labels are the only ones you may create by hand (`gh label create wayfinder:map`, and so on), the first time you need them.

- **Map**: an issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body.
- **Child ticket**: a sub-issue of the map, labelled `wayfinder:<type>` (`research`, `prototype`, `grilling` or `task`). Once claimed, it is assigned to the person driving.
- **Blocking**: blocked-by links (see **Commands**), or a `Blocked by: #<n>, #<n>` line where links aren't available. A ticket is unblocked when every blocker is closed.
- **Frontier**: the map's open sub-issues with no open blocker and no assignee. The first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, before any other write.
- **Resolve**: comment the answer, close the issue, then add a gist and link to the map's Decisions-so-far. An outcome that becomes real work becomes a normal planned issue.
