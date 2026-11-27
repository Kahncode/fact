# Issue tracker: GitHub Issues on `Kahncode/fact`

fact's own work is planned as GitHub issues on `Kahncode/fact` (`.fact/project.yaml` has `task_source: {kind: github}`). The factory's scheduler starts open issues labelled `fact:ready`, so the body and labels you write are what a run reads. Use the `gh` CLI for every operation; it infers the repo from `git remote -v`.

## Writing an issue (a ticket)

- **The spec lives in the issue body**, as the latest plan (`docs/spec/16-decisions.md`, "Where specs live"). Keep it current by editing the body, not by adding comments.
- **Body sections**, in this order: `## Goal`, `## Read first` (exact files and spec sections), `## Do`, `## Out of scope`, `## Acceptance criteria`. The factory reads the acceptance criteria from the `- [ ]` checklist under the `Acceptance criteria` heading, so every item must be checkable: an observable behavior a reviewer can confirm by a test, a `file:line` or a command. Which criteria get a test is the `testing` skill's call, not the issue's.
- **Labels**: one `fact:*` (below), one work type (`type:feature`, `bug`, `chore` or `incident`) and one `complexity:*` (`trivial`, `standard`, `complex`) for anything that runs; `type:epic` on parents (below); optionally one `priority:*` (`urgent`, `high`, `medium`, `low`; none means `medium`), which orders the scheduler's starts within the project and raises the issues it waits on. See `docs/agents/triage-labels.md`.
- **`fact:ready` or `fact:planned`** (until the M5 planner): a leaf issue, standalone or a sub-issue, is created `fact:ready`; a parent issue (one with sub-issues) is created `fact:planned`. The person approves a plan by setting its parent `fact:ready`; a sub-issue runs only once every ancestor is ready (`docs/spec/08-planning.md`, Parent issues and sub-issues). Create issues only after the person confirmed the breakdown in the conversation. Never set a parent `fact:ready` yourself.
- **Never add or remove `fact:needs-human` or `fact:running`**: the factory sets `fact:needs-human` and only a person clears it; `fact:running` is retired.
- **Never edit the text between `<!-- fact:context:start -->` and `<!-- fact:context:end -->`**: the factory owns that section.
- **Sizing a leaf issue**: one agent run, one PR a person can review in one sitting.
  - One concern per issue; size follows the concern, not a line count.
  - The acceptance criteria fit on one screen, and `## Read first` lists at most about six files or sections; otherwise split.
  - It ends green (lint, format, tests, CI) with its tests in the same PR, and can be done from its own body, `AGENTS.md` and `## Read first`.
  - Mostly a chain, with a side branch where two issues touch different files.
- **Parents**: work bigger than one leaf becomes a parent issue with sub-issues. A parent's open sub-issues are its dependencies. Once they are closed, a parent with a work type runs as a normal task (an integration or verification pass, with its own acceptance criteria and `complexity:*`); a parent without one is closed without a run.
- **Epics**: `type:epic` is for people; fact ignores it. Give it to every parent that holds a plan, and always to a parent of epics. A parent with work of its own carries `type:epic` and its work type; a parent with no work carries `type:epic` alone and no `complexity:*`.
- **Dependencies between leaves**: GitHub blocked-by links. If you can't add them, write a `Depends on #3, #4` line in the body. An issue runs only once everything it depends on is closed as completed. Don't add blocked-by links from a parent to its own sub-issues: being a sub-issue already makes it one.

## Commands

- **Create**: `gh issue create --title "..." --body-file <file> --label fact:planned --label type:feature --label complexity:standard`. Write the body to a file in the scratchpad and pass it with `--body-file`.
- **Read**: `gh issue view <n> --json number,title,body,labels,comments,state,stateReason`.
- **List**: `gh issue list --state open --label <label> --json number,title,labels`.
- **Sub-issue**: `gh issue create --parent <parent> ...`, or `gh issue edit <parent> --add-sub-issue <child>`.
- **Blocked-by**: `gh api --method POST repos/Kahncode/fact/issues/<n>/dependencies/blocked_by -F issue_id=<blocker database id>`. Get the database id with `gh api repos/Kahncode/fact/issues/<blocker> --jq .id` (not the `#number`).
- **Comment**: `gh issue comment <n> --body-file <file>`.
- **Labels**: `gh issue edit <n> --add-label ... --remove-label ...`.
- **Close**: `gh issue close <n> --reason "not planned" --comment "..."`. Close as completed only for work that is really done: the factory closes its own issues when their PRs merge, and a completed close satisfies dependencies.

## Overrides for the mattpocock skills

Where a skill's instructions differ from this file, this file wins.

- **`/grill-with-docs`**: write decisions into `docs/spec/` as they are made (`docs/agents/domain.md`), never into `GLOSSARY.md`, `docs/adr/` or `docs/decisions*.md`. Push the spec changes to `main` (`docs/agents/domain.md`, Planning goes straight to `main`) before creating any issue, so `## Read first` points at sections that exist and no run starts without them.
- **`/to-spec`**: publish one parent issue labelled `fact:planned`, never `fact:ready` (ignore the skill's `ready-for-agent`), and `type:epic`; if the parent has work of its own after its sub-issues, also give it its work type, a `complexity:*` and `## Acceptance criteria`. Point at the `docs/spec/` sections it relies on. Skip `/to-spec` for work that fits in one leaf issue.
- **`/to-tickets`**: pass the parent issue number; every ticket becomes its sub-issue. Write each ticket with this file's body sections (`## Goal`, `## Read first`, `## Do`, `## Out of scope`, `## Acceptance criteria`), not the skill's template, and do list exact files and spec sections in `## Read first`. Label each ticket `fact:ready` plus its `type:*` and `complexity:*` (and `priority:*` when it should not be `medium`), and add blocked-by links between tickets in dependency order.
- **`/implement` and `/implement-spec`**: not used for fact's own issues; the factory runs them.

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
