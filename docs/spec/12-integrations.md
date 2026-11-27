# Integrations

The issue tracker is where work starts and where its status is reported; GitHub is where code lands and humans review. Task ingestion is modular: markdown task files in the repository bootstrap fact until GitHub Issues is live; GitHub Issues is the first tracker, Linear the second, and others plug in behind the same interface.

**Task source interface**: `list_ready()`, `get(issue)`, `comment(issue, text)`, `set_labels(issue, add, remove)`, `write_context(issue, section)`, `link_prs(issue, prs)`, `close(issue)`. Issue keys carry their source, for example `gh:acme/app#123` or `lin:APP-123`, and branch names use a safe form such as `fact/gh-123`. In code: `fact.core.tasks.TaskSource`, chosen per project by `source_for`, with `deps_done(task)` and `mark_done(worktree, key)` besides the methods above. Two sources implement it: the file source (`task:M1-02`) and the GitHub Issues source (`gh:owner/repo#12`, `GitHubIssuesSource`, M2-09); the file source only logs its write-backs (its state lives in the task file), the GitHub Issues source writes them (M2-11). A run reports status changes through the `report_status` control activity: `run_started`, `gate_opened`, `gate_answered`, `prs_opened`, and the ends `merged`, `closed`, `stopped`. A `failed` or `needs_human` end reports nothing yet, so `fact:running` stays on the issue (target: see Status below).

**Where a project's issues live** (set in `.fact/project.yaml`):

- Single-repository project: that repository's own GitHub issues.
- Multi-repository project: a repository you point at to hold the tasks, by default the project's home config repository.
- Or a Linear team or project.

**GitHub Issues (first task source)**

- Trigger: open issues labelled `fact:ready` in the project's task repository, read by the scheduler's tick (`POST /api/v1/scheduler/tick`, in the API) with the GitHub App's token. In dev the API ticks every 30 s; once hosted an outside cron calls the tick, and GitHub webhooks will call it too.
- Reading (M2-09): type from the `type:*` label (`type:incident` runs as `bug` until incidents get their own type), complexity from `complexity:*`, acceptance criteria from the checklist under an "Acceptance criteria" heading. Dependencies are GitHub's blocked-by links plus `Depends on #3, owner/other#4` lines. A dependency is done when its issue is closed as completed (or closed before GitHub recorded a reason), or when it is a merged PR; closed as not planned or duplicate is not done, nor is an issue the App cannot read. Sub-issues, parents and epics (issue 47): an issue runs only when it and every ancestor carry `fact:ready`, its open sub-issues are dependencies (a closed one, whatever its close reason, is not), and a ready epic with nothing open is closed without a run; see Parent issues and sub-issues in `08-planning.md`.
- Status: GitHub issues are only open or closed, so progress shows as comments with the run and PR links, plus `fact:needs-human` on an issue that waits for a person. Target (decided 26 Nov 2026, not built yet): a run start only comments the run link; an open gate adds `fact:needs-human` and its answer through `fact gate` removes it; a run ending `failed` or `needs_human` adds it and comments why the run stopped; a blocker closed without completing adds it to the issue it blocks. No other code removes it, run ends included; a person removes it by hand. `fact:running` is retired: the runs table already knows what runs, and the label cost two API calls per run and went stale on every unreported end. Today, M2-11: a run start adds `fact:running` and comments the run link; an open gate adds `fact:needs-human` and comments what it needs, its answer removes the label; opened PRs are listed in one comment, and each PR body links the issue back (`Closes owner/repo#n`, or `Part of owner/repo#n` in a multi-PR change set), which shows the PR in the issue's Development sidebar; a `closed` or `stopped` end removes both labels and comments the end.
- Factory context: written into the issue body between `<!-- fact:context:start -->` and `<!-- fact:context:end -->`, so your own text stays untouched. The last start marker followed by an end marker delimits the section; with no such pair, a fresh pair is appended and stray markers stay. The body is re-read before each write and saved only when it changed. Nothing writes context yet.
- Closing: a single-PR change set closes its issue through GitHub: the PR body carries `Closes owner/repo#n` (always fully qualified, since the task repository can differ from the code repository), so GitHub closes the issue when the PR merges, even after the run that opened it stopped or failed. The factory still closes the issue itself (as completed) once the whole change set is merged, which covers multi-PR change sets (their PRs carry `Part of owner/repo#n`, which links without closing), a run ending `no_changes`, and an epic with no open sub-issue left; closing an issue GitHub already closed does no harm.
- Access: the fact GitHub App covers issues, PRs and checks.

**Linear (second task source)**

- Trigger: the factory picks up only issues labelled `fact:ready`, in a Linear team or project mapped to a factory project. Issues without it can be written, planned and refined freely. v0 polls the Linear API every 30 s; webhooks come with hosting. Triage then picks the project's workflow for the issue.
- On start: comment with the run link; move the issue to In Progress.
- On PRs opened: comment with the PR links; move to In Review.
- On merge of the whole change set: move to Done.
- On failure or a waiting gate: comment saying what is needed.

**Labels**, with the same names in every task source (label groups in Linear, plain labels in GitHub):

| Group | Labels | Set by | Meaning |
| --- | --- | --- | --- |
| fact | `fact:ready` | You, or the planner for leaf issues | The factory may pick this issue up (a sub-issue also needs every ancestor ready) |
| fact | `fact:planned` | The planner | Planned, not approved; a parent stays `fact:planned` until you approve its plan by setting it `fact:ready` (see `08-planning.md`) |
| fact | `fact:needs-human` | The factory; removed by you | Waiting on a person; the factory will not touch the issue until a person answers |
| fact | `fact:running` | The factory (retired, see Status) | A run is in progress |
| Type | `type:incident`, `type:bug`, `type:feature`, `type:chore`, `type:epic` | You or the planner | Picks the workflow in triage; one per issue. `type:epic` is a parent with no work of its own, closed when its sub-issues are |
| Complexity | `complexity:trivial`, `complexity:standard`, `complexity:complex` | You or the planner | How large or risky the change is; the default is standard |
| Priority | `priority:urgent`, `priority:high`, `priority:medium`, `priority:low` | You or the planner | Orders ready tasks within a project (see `08-planning.md`); the default is medium |

Linear has native priority (Urgent, High, Medium, Low, or No priority, which counts as medium) and fact reads that instead of a label. GitHub's native Priority issue field and native issue types (both org-oriented; `Kahncode/fact` is a personal repository) are not read for now; mapping them (Priority field values one to one, Bug→`bug`, Feature→`feature`, Task→`chore`) is a later option, listed in `16-decisions.md`.

`uv run fact github labels sync <owner/repo>` creates these labels on a repository (and fixes colour, description and case of existing ones; it never deletes labels).

Removing `fact:ready` from an issue with a run in progress does not stop the run; stopping it in the UI does.

**GitHub**

- Auth: a fact GitHub App from v0, installed on your repositories (contents, pull requests and issues read and write; checks and metadata read). PRs and comments are authored by `fact[bot]`, so you can review and approve them yourself.
- Branch `fact/<ISSUE-KEY>` in every touched repo; the factory never pushes to a default branch.
- PR description: the issue, the spec, what changed, test results, the run link, cost, and links to the sibling PRs.
- CI: poll the check runs and commit statuses of the PR's head commit until all finish; failures go to a fresh `fix_ci` agent session with the failing jobs' links and log tails, whose fix is pushed to the same PR before CI is awaited again (`until_ci_green`, see `06-workflow-authoring.md`).
- Comments: a PR comment that mentions the factory sends a signal to the run, which starts an address-comment session.
- Merge: done by the factory, with no agent, when the change set is classified auto\_merge and CI is green; otherwise you merge. The factory's own repo is classified like any other (`14-dogfooding-and-safety.md`).

**GitHub App setup (v0)**

| Setting | Value |
| --- | --- |
| Homepage URL | `https://github.com/Kahncode/fact` |
| Callback URL, OAuth during installation, device flow | Callback URL http://localhost:8000/github/callback (a placeholder GitHub requires; unused); OAuth during installation and device flow off |
| Webhook | Off in v0, which polls the same events through the API; on once fact is hosted (or locally through a smee.io forwarding channel) |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Checks, Commit statuses, Actions | Read-only |
| Metadata | Read-only (mandatory) |
| Workflows | Read and write (agents may change any file, including `.github/workflows/`) |
| Everything else, organization and account permissions | No access |
| Events | Subscribed now, delivered once the webhook is on: Issues, Issue comment, Label, Sub issues (if listed), Pull request, Pull request review, Pull request review comment, Pull request review thread, Check run, Check suite, Status, Workflow run, Push |
| Installable by | Only this account |

After creating it: generate a private key (kept outside any repository), note the App ID, and install it on selected repositories only (Kahncode/fact first). The worker reads `FACT_GITHUB_APP_ID` (the App ID or the Client ID; GitHub accepts either as the JWT issuer) and `FACT_GITHUB_APP_PRIVATE_KEY_PATH` (a relative path resolves from the folder holding `fact.yaml`) from `.env`. Installation tokens are minted per repository and can reach only that repository. `uv run fact github check <owner/repo>` verifies the setup.
