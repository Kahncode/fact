# Workflow authoring

Workflows are Python code built from a small library of reusable steps. Prompts and settings are data, so many changes need no code change at all.

**Three layers, changed at three speeds**

| Layer | Lives in | Example change | Takes effect |
| --- | --- | --- | --- |
| Config and prompts | `.fact/project.yaml` and `.fact/prompts/` in the project's home repository; `.fact/repo.yaml` in each repository | Better implement prompt, new test command, cheaper model | Next step that reads it, even in a running run |
| Step library | Activities in `fact.worker`; workflow helpers in `fact.sdk` call them by name through `fact.core.activity_contract` | New `run_e2e` step, better PR description | New activity attempts once the worker restarts |
| Workflow code | `.fact/workflows/` in the project's home repository | Add a review stage, change a loop | New runs only; runs in flight finish on their pinned version |

**Step library for v0**: `agent_step`, `shell_step`, `prepare_workspace`, `open_change_set`, `wait_for_ci`, `human_gate`, `notify`, `issue_update`. Bookkeeping activities that are not reported as steps: `mark_task_done`, `post_cost_comment`, `record_run_started`, `record_run_finished`, `teardown_workspace`. `FactWorkflow.execute` tears the workspace down when the run ends `pr_opened`, `merged`, `closed` or `abandoned`, once its work is pushed. Any other end leaves it on disk for a person, still holding its branch and unpushed commits; nothing removes it automatically. `prepare` runs once per run: called again it returns the same workspace, so a run's branches and PR never change.

**Stage manifest**: each workflow class declares its stages as a static list (id, label, kind, and loop-back edges). The UI draws the expected graph from it and lays the run's real steps over it. Temporal itself has no static graph of a code workflow, so this is how R12 is met. In code: `@stages([Stage(id, label, kind, loops_to=...)])` from `fact.sdk.workflow`.

**Changing a workflow**

1. Open an issue against the factory project; the factory itself can do the work.
2. The PR's CI runs unit tests plus Temporal replay tests against recorded histories of recent runs.
3. You merge. `fact use main`, then restarting `fact dev --pinned`, starts a worker on the new version (`fact upgrade` will do both later).
4. New runs use the new version. Runs in flight stay pinned to the old worker until they finish, then it stops.

In v0, if nothing is in flight, the upgrade is a plain restart.

### Per-project workflows and triage

Each project has its own catalog of workflows, and a triage step picks one for every incoming issue. An incident fix and a feature in the same project can follow different paths.

**Workflows are Python, one or more per project.** A project's workflows live in `.fact/workflows/*.py` in the project's home repository, never in the factory repo, and are built from the factory's step library. A decorator records each workflow's stages so the UI can draw it.

**Where projects live.** Each project has a home repository with a `.fact/` folder: `project.yaml` (its repositories and merge order, task source, runner pool, budgets, merge policy, triage rules), `workflows/*.py` and `prompts/`. The factory repo holds only the factory, its step library (`fact.sdk`) and a registry in `fact.yaml` mapping each project to its home repository.

- **Single-repository project**: the home is that repository.
- **Multi-repository project**: the home is a small dedicated config repository (for example `<product>-fact`) by default, so changing a workflow never needs a PR on product code. Pointing the home at one of its repositories, such as the backend, is also allowed.
- **Per-repository commands** (setup, lint, test, build) live in each repository's own `.fact/repo.yaml`, so they change together with that repository's code. `setup` runs once per run, right after the workspace is prepared; fact's own setup runs `uv sync` and `pre-commit install --install-hooks`, so commits in a workspace run the repository's hooks.
- **Loading and pinning**: each project gets its own workflow worker and task queue (`wf-<project>`), which loads its workflows from a checkout of the home repository at a fixed SHA. A run is pinned to that SHA plus the factory version. Activities still run in the control and sandbox zones.
- **Changing a project's workflows** is a PR on its home repository, which the factory can make itself like any other change.
- **Trust**: project workflow code runs in a factory worker, so only projects you own get workflow code; a client project could later be limited to configuration only.

**Versions are pinned, with no upgrades.** A run always finishes on the workflow version it started on. To move a run to newer code, you drop it in the UI and restart it on the latest version. Dropping keeps the run's branches and PRs in every repository, so the new run picks up where the old one left off: it starts from those branches, reuses the open PRs, and gets a handoff note with the spec, the last step reached and a summary of the old run's transcripts. Each worker version keeps running until its last run finishes or is dropped, and the UI lists versions with their run counts.

**How pinning is built (M1-13): Worker Deployment Versioning.** temporalio 1.36 and the Temporal server (the 1.32 dev server included) support Worker Deployment Versioning with a pinned behaviour, so fact uses it rather than only recording versions:

- `python -m fact.worker` loads each registered project: it syncs the home repository's mirror (`repos_dir`), resolves the default branch's SHA, checks it out as a detached worktree at `pinned_dir/projects/<project>/<sha>` (reused when present) and imports `.fact/workflows/*.py` (files starting with `_` are skipped) under module names unique to the project and SHA. A project that fails to load is logged with the failing file and skipped; the others still start.
- Each project is one worker deployment named `wf-<project>`, polled on task queue `wf-<project>`. The build id is the version id `<factory SHA, 12 chars>+<home SHA>` (it fits the 64-character `worker_version` column; a factory that is not a git checkout is `unknown`). The default behaviour is `PINNED`, and on start the worker makes its version the deployment's current one, so new runs go to it; startup fails if it cannot.
- Every run records its version: `FactWorkflow.execute` reads the build id from `workflow.info().get_current_deployment_version()` and passes it to `record_run_started`, which stores `worker_version` and `home_sha`.
- One worker process runs only the newest version. After a restart on new code, runs pinned to an older version wait for a worker of that version: drop and restart them. Running several versions side by side (`fact upgrade`) comes later.

**Long human waits are fine.** A gate is a Temporal wait on a signal or update. It can last weeks, costs nothing while waiting, and survives restarts. The only cost is that its worker version has to keep running until the run ends.

**Planning comes first.** Refresh, grilling, triage, the plan and the subtask breakdown all happen in the Planning phase (below). An execution workflow starts only once its issue holds an approved, or auto-approved, plan.

**Example: two workflows in one project**

| Stage | `feature` | `incident-fix` |
| --- | --- | --- |
| Plan approval gate | Yes | Skipped |
| Implement → tests | Yes | Yes |
| Deploy preview | No | Yes: the project's preview command (for example a staging deploy or a preview build) |
| Gate | Approve spec | "Verified on preview", with the preview link |
| PRs → CI → review → merge | Yes | Yes, with a higher notification priority |

Preview deploys are never left to an agent: each project configures them as steps with its own conditions (some projects get previews from external tools when a dev branch is pushed). They will be designed when the first project needs them.

### Steps and acceptance

Checks are run by the factory with the project's own frameworks, never reported by an agent, and each failure is fed to a new agent session in the same sandbox. Everything up to a clean, reviewed change happens before any PR is opened.

```python
@workflow.defn
class Standard(FactWorkflow):
    stages = ["implement", "checks", "review", "learn", "classify", "open_prs", "ci", "merge"]

    @workflow.run
    async def run(self, issue: Issue) -> RunResult:
        await self.agent("implement", prompt="implement.md")
        await self.until_green(["lint", "test"], fix_prompt="fix_checks.md", max_attempts=3)

        review = await self.agent("review", prompt="review.md")
        if review.findings:
            await self.agent("address_review", prompt="address_review.md", context=review)
            await self.until_green(["lint", "test"], fix_prompt="fix_checks.md", max_attempts=3)

        await self.agent("learn", prompt="learn.md")
        route = await self.classify()               # "auto_merge" or "human_review"
        prs = await self.open_change_set(label=route)
        await self.until_ci_green(prs, fix_prompt="fix_ci.md", max_attempts=2)

        # Open until merged or closed; each @fact comment starts a new session
        async for comment in self.comments_until_merged(prs, auto_merge=(route == "auto_merge")):
            await self.agent("address_comment", prompt="address_comment.md", context=comment)
            await self.until_green(["lint", "test"], fix_prompt="fix_checks.md", max_attempts=3)
            await self.push(prs)
            await self.until_ci_green(prs, fix_prompt="fix_ci.md", max_attempts=2)
```

The example is the target shape. What exists today (M1, plus M2-01 gates) is the shorter `Standard` in `.fact/workflows/standard.py`: prepare, setup (each repository's `setup` commands, such as installing dependencies and git hooks; a failure ends the run `failed`), implement, `until_green`, review and address review, learn, mark the task done, open the PR, comment its cost, `until_ci_green` (at most 2 `fix_ci` sessions), then update the cost comment and notify how the run ended; stages come from `@stages`, and the run method delegates to `FactWorkflow.execute`. There is no classify, merge or comment stage yet.

- **Checks**: `until_green` runs the project's lint and test commands (for example `ruff`, `pytest`) as shell steps in the run sandbox. On failure, the output goes to a fresh fix session (a new attempt of the `implement` stage); `max_attempts` caps the fix sessions, then the run pauses and notifies you. Since M2-01 `Standard` opens a `needs_human` gate there: approve runs the loop once more, reject ends the run `needs_human` with no PR. Since M2-03 the gate notifies you (`gate_waiting`, once per gate), and `Standard` notifies when the run ends: `prs_ready` with CI green, `run_failed` when it ends `failed` or `needs_human` (see `11-ui-and-notifications.md`).
- **CI**: `until_ci_green(prs, fix_prompt, max_attempts)` runs `wait_for_ci` on each PR's head commit: a control step (stage `wait_for_ci`, kind `github`) that polls check runs and commit statuses every 30 s until every check is complete on two polls in a row, or 60 minutes pass. A repository with no checks at all after 5 minutes passes. Failed GitHub Actions job logs go to the artifact store, and their last 4 KB to a fresh `fix_ci` session (a new attempt of stage `fix_ci`, which loops to `open_pr`); its commit is pushed as the next `open_pr` attempt, and CI is awaited again. `max_attempts` caps the fix sessions. The loop gives up (`needs_human`; `Standard` then opens a `needs_human` gate, whose reject ends the run with the PR left open) when the fixes are exhausted, a fix session does not pass or commits nothing, or CI never finishes.
- **Sessions**: each agent role (implement, fix, review, address review, learn, address comment) is its own session, in the same sandbox and on the same branches.

  **Learn**: after the review and before classifying, an agent session runs the `learn` skill to update the repository's context files (`CLAUDE.md` or `AGENTS.md`, `docs/`) to match what the change did, as part of the same branch. Context lives entirely in the repositories for now. The same session writes the PR title and summary (its `title` and `summary` answer), which open the PR description; without them the PR keeps the implement session's text.
- **Classify**: rules first (project policy, paths touched, diff size; the factory's own repo is always `human_review`), then an LLM classifier for what the rules leave open. The result is a label on every PR of the change set.
- **Merge**: in v0 you always merge; the classification (high or low risk, from the review agent and the rules) is shown as a label and in the inbox. From a later version, a project can opt in to automatic merges for low-risk change sets: with no agent involved, fact checks that CI passed on the exact head commit, then merges in the recorded repository order.
- **Comments**: a PR comment that mentions the factory sends a signal to the run, which stays open until its PRs merge or close. The fact GitHub App sees every comment; v0 finds them by polling with the App's token (no public URL needed), webhooks come once fact is hosted. Comments may use `/fact` or @fact.

### Review evidence and extra gates

Every PR carries the evidence a reviewer needs, built by the factory from run data rather than written by an agent, and extra gates plug in as more checks with capped fix loops.

| Item | What it is | Phase |
| --- | --- | --- |
| Scripted PR description | Title and summary written by the agent as a step output; the rest (issue, refreshed context, check results, run link, sibling PRs) filled in from run data by a template | v0 |
| Cost comment | A PR comment with time and cost per step and per harness and model, updated in place (marker `<!-- fact:cost -->`); `post_cost_comment` right after `open_change_set`, and again at the end of the run | v0 |
| Review checklist per project | `.fact/prompts/review.md` with the project's rules; always includes "does the change meet the issue's acceptance criteria" | v0 |
| Head-SHA guard | Before an automatic merge, confirm CI passed on exactly the commit being merged | v0 |
| Screenshots | For UI projects, a Playwright screenshot of every screen the change touches, attached to the PR and stored in object storage | Later |
| Preview link | A running preview of the change linked from the PR, using the project's preview deploy command | Later (v0 only for `incident-fix`) |
| Architecture diff | A generated before-and-after diagram of the components the change touches | Later |
| Static analysis | SonarCloud (or similar) findings read from the PR's checks, fed to a fix session, at most 3 passes | Later |

All evidence files go to object storage; the PR and the run graph link to them.
