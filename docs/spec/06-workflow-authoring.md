# Workflow authoring

Workflows are Python code built from a small library of reusable steps. Prompts and settings are data, so many changes need no code change at all.

**Three layers, changed at three speeds**

| Layer | Lives in | Example change | Takes effect |
| --- | --- | --- | --- |
| Config and prompts | `.fact/project.yaml` and `.fact/prompts/` in the project's home repository; `.fact/repo.yaml` in each repository | Better implement prompt, new test command, cheaper model | Next step that reads it, even in a running run |
| Step library | The factory's step types: activities in `fact.worker`, declared in `fact.core.activity_contract` and called through `fact.sdk` | New `run_e2e` step type, better default PR template | New activity attempts once the worker restarts |
| Project step types | `Script` steps declared in `.fact/workflows/`, their code in `.fact/steps/` (or anywhere in the repository), run in the run's runner | A deploy-preview step, the project's own PR description | New runs of the next config version |
| Workflow code | `.fact/workflows/` in the project's home repository | Add a review stage, change a loop | New runs only; runs in flight finish on their pinned version |

**Step library for v0**: `agent_step`, `shell_step`, `prepare_workspace`, `open_change_set`, `wait_for_ci`, `wait_for_review` (its step is recorded by the workflow as a gate-like step), `converse`, `human_gate`, `notify`, `issue_update`. Bookkeeping activities that are not reported as steps: `mark_task_done`, `post_cost_comment`, `record_run_started`, `record_run_finished`, `teardown_workspace`. `FactWorkflow.execute` tears the workspace down when the run ends `pr_opened`, `merged`, `closed` or `abandoned`, once its work is pushed. Any other end leaves it on disk for a person, still holding its branch and unpushed commits; nothing removes it automatically. `prepare` runs once per run: called again it returns the same workspace, so a run's branches and PR never change.

**Stage manifest**: each workflow class declares its stages as a static list (id, label, kind, and loop-back edges). The UI draws the expected graph from it and lays the run's real steps over it. Temporal itself has no static graph of a code workflow, so this is how R12 is met. In code: `@stages([Stage(id, label, kind, loops_to=...)])` from `fact.sdk.workflow`.

**Workflow design principles**

- **No unnecessary work.** A step runs only when the current state calls for it, not in case it might be needed.
- **No artificial waiting.** Prefer events to polling: signals, and webhooks once fact is hosted (v0 polls GitHub, see "Comments" below). Where a run must poll, the interval is short, so a change is seen soon after it happens.
- **One source of truth.** Read state from the system that owns it (for a PR, GitHub: mergeability, checks, review), rather than keeping a copy in the workflow or predicting it.
- **A simple graph.** A workflow is a small graph of states that loop back over the same steps, with few edge cases and few side effects outside its steps. A new situation reuses an existing loop where it can.
- **Survives a restart.** A run and its activities stay correct when a worker or process restarts and a retry lands elsewhere. State lives in Temporal, the database, the artifact store or on disk (git's own state in the worktree), never in process memory such as per-run lock dicts; coordination uses heartbeats with heartbeat timeouts, `WAIT_CANCELLED` cancellation, idempotent activities that resume partial work, and the external system's own locks (git `index.lock`).

Example: a PR that conflicts with its base. The run merges the base into its branch only when GitHub reports the PR `dirty` (read by `wait_for_ci`) or refuses the merge as out of date. It then goes through the existing CI loop (sync, push, wait again). It never merges `main` because `main` moved, and it has no separate pre-PR sync path.

### The workflow SDK: what the framework owns, what the project owns

The framework (`fact.sdk` and the worker) runs every workflow the same way. The project writes only its software development process: which steps run, in what order, and how they loop. Agents write most workflow code, so the SDK makes the correct path the short one, and leaves out whatever a project should never touch.

**Who owns what**

| Framework | Project |
| --- | --- |
| The run lifecycle (lifecycle steps): recording the run's start and end, task reporting, stop and handoff, workspace teardown, the cost comment, run-end notifications | The graph: which steps run, their order, branches, loops and the conditions between them |
| Activity mechanics: queues and routing, timeouts, heartbeats, retries, cancellation | Prompts, check commands, the schemas of agent answers |
| Step records: stage ids, attempt numbers, logs and transcripts in the artifact store, tokens, cost, wall and wait time | Fix and retry counts, `max_duration` per step |
| Payload size limits and trimming text | Where gates go and what they ask |
| Gate mechanics: gate ids, gate notifications, task-source labels, delivering steering | Extra notifications a step sends, through the framework's notification step |
| Workspace mechanics: prepare, branches, the teardown policy | Which repositories and which runner a step needs |
| Push and merge mechanics, the head-SHA guard, the PR footer (run link, markers) | Merge policy, classify rules, the PR description (template and any logic, by replacing the `describe` slot) |
| Usage pauses, budgets and caps, turning failures into outcomes, outputs, versions | The harness and model for each stage |

**Shape of a workflow.**
- **Lifecycle:** the framework owns `@workflow.run`, and runs the lifecycle steps around the project's `steps()` method.
- **Stages:** a workflow declares its stages as class attributes, and the stage manifest is derived from them, so stage ids are never written twice.
- **State:** the run holds the framework's state, which steps find themselves:
  - its default workspace, prepared the first time a step needs it;
  - its change set.

  The data the project decides with flows through the steps' return values.
- **What a project never does:** pass a workspace or the PRs around, set Temporal options, or catch Temporal exceptions.
- **A step that does not pass** ends the run with the matching outcome, unless the step is `required=False` or the project handles its result.

```python
from fact.sdk import Agent, ChangeSet, Checks, Script, Workflow, workflow

@workflow("standard")
class Standard(Workflow):
    implement = Agent("Implement", prompt="implement")
    checks    = Checks("Lint and test", ["lint", "test"], fix=implement, max_fixes=3)
    review    = Agent("Review", prompt="review", answer=Review)
    address   = Agent("Address review", prompt="address_review", then=checks)
    learn     = Agent("Learn", prompt="learn", required=False)
    ship      = ChangeSet(ci_fix=Agent("Fix CI", prompt="fix_ci"), max_ci_fixes=2, on_green="merge")

    async def steps(self) -> None:
        await self.implement()
        await self.checks()
        review = await self.review()
        if review.answer.needs_changes:
            await self.address(context=review)
        await self.learn()
        await self.ship(criteria=review.answer.criteria)
```

**Step types.** A step type comes from the factory's step library (`Agent`, `Checks`, `ChangeSet`, gates, notifications) or is one of the project's own (`Script`). Composite library steps (`Checks`, `ChangeSet`) carry the framework's opinions, such as merging the base only when GitHub says the PR cannot merge. They are built from named slots, and the project can replace any slot with a step type of its own:
- `ChangeSet`: `describe` (PR title and body), `open`, `ci_fix`, `merge`.
- `Checks`: `run`, `fix`, `on_exhausted`.

**Project step types run outside the factory.** A `Script` step is declared with a label, a command, typed input and output (Pydantic models), and optionally `max_duration` and `workspace=False`:

```python
deploy = Script("Deploy preview", run=["uv", "run", ".fact/steps/deploy.py"],
                input=DeployIn, output=DeployOut, max_duration="40m")
result = await self.deploy(DeployIn(env="dev"))   # a DeployOut
```

A Script step runs in the run's runner, through one generic framework activity, never inside a factory process. It uses the project's own environment, can be in any language and gets only the runner's secrets. The step protocol (`09-runners-and-harnesses.md`) is how it talks to fact.

**Outputs.**
- **Outcomes are generic:** a run ends `succeeded`, `nothing_to_do`, `failed`, `needs_human` or `stopped`.
- **What the run produced** is a list of typed outputs, which its steps record: a change set (with its own state: open, merged, closed), tasks created in the tracker, a deployment and its URL. A run that found nothing to do has none.
- **A workspace is a resource** a step asks for and the framework releases, so a workflow without one (triage, tracker updates) is valid.
- **External tools:** fact triggers them and watches them; it never reimplements them.

**What a project workflow may use.** Only `fact.sdk`, one public module that also provides the `workflow` decorator. A project workflow never imports `temporalio`, `fact.core` or `fact.sdk` internals, and never calls `execute_activity`.

**Versions.**
- fact and the SDK share one semver version, starting at 0.1.0. Before 1.0, a breaking change moves the minor version and a compatible one moves the patch.
- A change that affects projects moves the version: the public `fact.sdk` API, the step protocol, the `.fact/project.yaml` schema, prompt variables. A committed SDK API snapshot, checked by a test like `api/openapi.json`, makes a forgotten bump fail.
- A project declares the fact range it works with in `.fact/project.yaml` (`fact: "~0.1"`).
- The project config version is the git tree hash of `.fact/`: workflows, prompts and config, nothing else.
- A run's version is `<fact version>+<.fact tree hash>`, and the run also records the home SHA it loaded from. Benchmarks compare config versions with this id.
- A run's activities run on workers of the same factory build as its workflow.
- When `.fact/` changes on a project's default branch, the running worker loads the new config version alongside the old one and sends new runs to it; the old one serves its runs until they end. A new fact version still needs a restart.
- No git tags before 1.0.

**Checking a project's workflows.** `python -m fact.sdk.check .fact` checks that:
- the workflows import, and import only what they may;
- the declared range includes this fact version;
- the stage manifest can be derived;
- every prompt and script they name exists;
- the step declarations are valid.

It runs in three places:
- In the project's CI, through a reusable GitHub Action or the same `uvx` command, against fact `main` until 1.0.
- Inside a run whose change touches `.fact/`, as one of its checks, in the workspace and against the fact version that change targets. In fact's own repository that is the workspace's own SDK, so a run can make a breaking change to the SDK and the workflows that use it together.
- When the worker loads a config version. A version that fails is refused:
  - its runs do not start, and the scheduler holds its tasks (`incompatible`);
  - an `error` notification says why;
  - every other project and version keeps running.

**Several machines.** Steps reach a workspace through its handle, which names the task queue its runner polls, so no workflow code assumes one machine. Moving a run to other compute is therefore a change in which queue its steps go to. Running across several runners, rebuilding a lost workspace and several workspaces per run come in M7 (`tasks/backlog.md`, Distributed runs).

**Changing a workflow**

1. Open an issue against the factory project; the factory itself can do the work.
2. The PR's CI runs unit tests plus Temporal replay tests against recorded histories of recent runs.
3. You merge. `fact use main` pins the new version and `fact dev` starts a worker generation on it next to the old ones; `fact dev --follow main` does both by itself when `main` moves, without waiting for runs to end (`14-dogfooding-and-safety.md`, Upgrading while it runs).
4. New runs use the new version. Runs in flight stay pinned to the old generation until they finish, then its worker stops.

### Per-project workflows and triage

Each project has its own catalog of workflows, and a triage step picks one for every incoming issue. An incident fix and a feature in the same project can follow different paths.

**Workflows are Python, one or more per project.** A project's workflows live in `.fact/workflows/*.py` in the project's home repository, never in the factory repo, and are built from the factory's step library. A decorator records each workflow's stages so the UI can draw it.

**Where projects live.** Each project has a home repository with a `.fact/` folder: `project.yaml` (its repositories and merge order, task source, runner pool, budgets, its concurrency cap, routing, step limits, merge policy, triage rules), `workflows/*.py` and `prompts/`. The factory repo holds only the factory, its step library (`fact.sdk`) and its factory settings in `fact.yaml` (`05-domain-model.md`), including the registry mapping each project to its home repository. Step limits (timeout, turns, permission mode per step kind) have defaults in the SDK's code; a project's `step_defaults` overrides any of them.

- **Single-repository project**: the home is that repository.
- **Multi-repository project**: the home is a small dedicated config repository (for example `<product>-fact`) by default, so changing a workflow never needs a PR on product code. Pointing the home at one of its repositories, such as the backend, is also allowed.
- **Per-repository commands** (setup, lint, test, build) live in each repository's own `.fact/repo.yaml`, so they change together with that repository's code. `setup` runs once per run, right after the workspace is prepared; fact's own setup runs `uv sync` and `pre-commit install --install-hooks`, so commits in a workspace run the repository's hooks.
- **Loading and pinning**: each project gets its own workflow worker and task queue (`wf-<project>`), which loads its workflows from a checkout of the home repository at a fixed SHA. A run is pinned to that SHA plus the factory version. Activities still run in the control and sandbox zones.
- **Changing a project's workflows** is a PR on its home repository, which the factory can make itself like any other change.
- **Trust**: project workflow code runs in a factory worker, so only projects you own get workflow code; a client project could later be limited to configuration only.

**Versions are pinned, with no upgrades.** A run always finishes on the workflow version it started on. To move a run to newer code, you stop it in the UI and restart it on the latest version. Stopping keeps the run's branches and PRs in every repository, so the new run picks up where the old one left off: it starts from those branches, reuses the open PRs, and gets a handoff note with the spec, the last step reached and a summary of the old run's transcripts. Each worker version keeps running until its last run finishes or is stopped, and the UI lists versions with their run counts.

**What exists today (M2-06), from the CLI.** `fact stop <run> [--reason TEXT]` (`POST /api/v1/runs/{id}/stop`) cancels the workflow with the reason. `FactWorkflow` handles the cancellation itself (the `StopSteps` mixin, `fact.sdk.stop`): no new step starts, a running agent or shell step is stopped, then it writes a handoff note to the artifact store (`runs/<run>/handoff.md`: the reason, the last stage reached, the PR links and each agent step's summary), tears the workspace down, and records outcome `stopped` last; the workflow completes normally with that outcome. The remote branch and the PR stay. `fact run` then starts the task on the newest version: the workspace resumes the task branch from `origin`, `open_change_set` reuses its open PR, and `POST /api/v1/runs/start` passes the newest note as `RunInput.handoff_ref`, rendered into prompts as `$handoff`. Commits the stopped run never pushed are lost. The cancellation is processed by the run's own pinned worker version: a run pinned to a version no worker runs (or started before M2-06) never ends `stopped`; stop it, then delete its `<data_dir>/workspaces/<run_id>` by hand.

**How pinning is built (M1-13): Worker Deployment Versioning.** temporalio 1.36 and the Temporal server (the 1.32 dev server included) support Worker Deployment Versioning with a pinned behaviour, so fact uses it rather than only recording versions:

- `python -m fact.worker` loads each registered project: it resolves the home repository's default branch SHA (`git ls-remote`), clones the repository detached at that SHA into `pinned_dir/projects/<project>/<sha>` (reused when present) and imports `.fact/workflows/*.py` (files starting with `_` are skipped) under module names unique to the project and SHA. A project that fails to load is logged with the failing file and skipped; the others still start.
- Each project is one worker deployment named `wf-<project>`, polled on task queue `wf-<project>`. The build id is the version id `<factory SHA, 12 chars>+<home SHA>` (it fits the 64-character `worker_version` column; a factory that is not a git checkout is `unknown`). The default behaviour is `PINNED`, and on start the worker makes its version the deployment's current one, so new runs go to it; startup fails if it cannot.
- Every run records its version: `FactWorkflow.execute` reads the build id from `workflow.info().get_current_deployment_version()` and passes it to `record_run_started`, which stores `worker_version` and `home_sha`.
- Several versions run side by side: one worker process per worker generation, kept while Temporal reports any of its versions `DRAINING`, so a run pinned to an older version always has its worker, across restarts and reboots too (`14-dogfooding-and-safety.md`, Upgrading while it runs). Today one worker process runs only the newest version, and runs pinned to an older one stall after a restart on new code.
- `fact dev --follow <ref>` upgrades by itself: once `<ref>` (or a project's home branch) moves, it pins `<ref>`, restarts the API and starts the new generation next to the old ones, whatever runs are active. `--on commit` (the default) upgrades on every new commit; `--on version` upgrades only when the fact version (semver) at `<ref>` differs from the pinned one.

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

        # Open until merged or closed; each @sofact-dev comment starts a new session
        async for comment in self.comments_until_merged(prs, auto_merge=(route == "auto_merge")):
            await self.agent("address_comment", prompt="address_comment.md", context=comment)
            await self.until_green(["lint", "test"], fix_prompt="fix_checks.md", max_attempts=3)
            await self.push(prs)
            await self.until_ci_green(prs, fix_prompt="fix_ci.md", max_attempts=2)
```

The example is the target shape. What exists today (M1, plus M2-01 gates) is the shorter `Standard` in `.fact/workflows/standard.py`: an optional `approve_plan` gate (when `RunInput.require_plan_approval` is on; reject ends the run `stopped`), prepare, setup (each repository's `setup` commands, such as installing dependencies and git hooks; a failure ends the run `failed`), implement, `until_green`, review and address review, learn, mark the task done, open the PR, `until_ci_green` (at most 2 `fix_ci` sessions; a PR GitHub reports as conflicting with its base gets the base merged instead, `sync_with_base`: a merge commit, conflicts resolved by a `resolve_conflicts` session, then the checks and a push), then (since M2-05) notify `prs_ready` and wait for review (stage `wait_for_review`; GitHub's review flow since #94, below) until every PR is merged or closed, ending `merged` (all merged) or `closed`; stages come from `@stages`, and the run method delegates to `FactWorkflow.execute`, which posts the cost comment once at the end (see the table below). Since #13, once CI is green, `Standard` classifies the change set (`FactWorkflow.classify`: stages `classify`, the read-only session, and `label_prs`, which labels the PRs, adds the `## Review` section and records the classification in its step outputs). There is no comment stage yet. Since #14 the merge follows the classification: a change set routed `auto_merge` in a project whose `auto_merge` is on (`RunInput.auto_merge` and `merge_method`, from `.fact/project.yaml`) is merged by fact through the `merge_change_set` control activity, which checks that each PR's head is still the commit CI passed on and that CI there is green, then merges with the project's `merge_method` in the recorded repository order; the run ends `merged` and sends no notification at all (no `prs_ready`, no `run_finished`). If GitHub refuses the merge because the base moved on (the PR is `dirty` or `behind`), it merges the base, pushes, waits for CI and classifies again, at most twice; a change set routed `human_review`, auto-merge off, any other refusal or the cap relabels the PRs `fact:needs-review` with why, sends `prs_ready` with the classification's reasons and waits for review.

- **Checks**: `until_green` runs the project's lint and test commands (for example `ruff`, `pytest`) as shell steps in the run sandbox. On failure, the output goes to a fresh fix session (a new attempt of the `implement` stage); `max_attempts` caps the fix sessions, then the run pauses and notifies you. Since issue 95 `Standard` ends the run `needs_human` there, with no gate and no PR (a gate opens when a blocked agent asks a `question`, `07-reference-workflow.md`). Since M2-03 a gate notifies you (`gate_waiting`, once per gate), and `Standard` notifies when the run ends: `run_failed` when it ends `failed` or `needs_human`, `run_finished` for `merged`, `closed` or `stopped`. Since M2-05 `prs_ready` comes once CI is green, before the wait for review (see `11-ui-and-notifications.md`).
- **CI**: `until_ci_green(prs, fix_prompt, max_attempts)` runs `wait_for_ci` on each PR's head commit: a control step (stage `wait_for_ci`, kind `github`) that polls check runs and commit statuses every 30 s until every check is complete on two polls in a row, or 60 minutes pass. While no check has appeared, each poll reads the PR's mergeability: GitHub runs no CI on a PR that conflicts with its base, so a conflict ends the wait at once (`until_ci_green` returns `conflict`) and the workflow merges the base (`sync_with_base`), pushes and waits again. The base is never merged just because it moved on: only GitHub's answer decides. A repository with no checks at all after 5 minutes on a PR that does not conflict passes. Failed GitHub Actions job logs go to the artifact store, and their last 4 KB to a fresh `fix_ci` session (a new attempt of stage `fix_ci`, which loops to `open_pr`); its commit is pushed as the next `open_pr` attempt, and CI is awaited again. `max_attempts` caps the fix sessions. The loop gives up (`needs_human`; `Standard` then ends the run `needs_human` with the PR left open, no gate) when the fixes are exhausted, a fix session does not pass or commits nothing, or CI never finishes.
- **Sessions**: each agent role (implement, fix, review, address review, learn, address comment) is its own session, in the same sandbox and on the same branches.

  **Learn**: after the review and before classifying, an agent session runs the `learn` skill to update the repository's context files (`CLAUDE.md` or `AGENTS.md`, `docs/`) to match what the change did, as part of the same branch. Context lives entirely in the repositories for now. The same session writes the PR title and summary (its `title` and `summary` answer), which open the PR description; without them the PR keeps the implement session's text.
- **Classify**: rules first (project policy, paths touched, diff size), then an LLM classifier for what the rules leave open. The result is a label on every PR of the change set.

  **Classify, first version (decided 26 Nov 2026, M3; built in #13, merging by the route in #14, checked end to end in #7).** Once classify exists, a human review is no longer the default: a change set needs one only when a signal says so. The signals:
  - **Ambiguous spec and assumptions**: the implementation session says whether it found the spec ambiguous and lists the assumptions it made; both go in the PR description so a person can review them.
  - **Security**: a security check of the change.
  - **Database schema changes.**
  - **API surface changes.** A published API never changes: a deterministic test fails the build when a previously published route is changed or removed.
  - **Risk and reversibility**: an LLM classifier rates the change high or low risk, and says whether it is a one-way door (hard to undo, so more committing and more important to review) or a two-way door.

  How it works:
  - **When**: once the change set is ready to go, after CI is green, so the whole change can be judged. Order: open PRs, CI green, classify, then merge or wait for review.
  - **Per project**: classify is a project step with its own prompt (`.fact/prompts/classify.md`) and its own deterministic checks. Path globs in `.fact/project.yaml` (`classify: {schema_paths, api_paths, always_review_paths}`, such as `.fact/**`, `.github/**`) raise signals with no LLM. Built: the `classify_signals` control activity raises `schema_change`, `api_change` and `always_review` from changed paths, `api_breaking` when a JSON file under `api_paths` (the OpenAPI snapshot) loses or changes anything (additions are `api_change` only), and `task_closed` when the task source says the task was closed during the run. It reads the globs from the project's registered checkout, never the run's branch. `auto_merge` (default true) and `merge_method` (default `squash`) are parsed in `ProjectConfig` and passed to the run in `RunInput`. Built (#13, #14): the session, the decision (`fact.sdk.classification.decide`, which also routes to `human_review` when the session or the signals give no valid answer) and the output below.
  - **The LLM part**: one read-only agent session gets the diff, the task and its acceptance criteria, the review result and the project's prompt, and answers `risk` (high, low), `door` (one-way, two-way) and `security_findings`, each with a reason.
  - **Decision**: `human_review` if any signal holds: the implementation found the spec ambiguous (unless the project sets `classify.review_spec_ambiguous: false`; the session still sees it), a security finding, a schema change, an API surface change, high risk, a one-way door; else `auto_merge`. Assumptions alone do not force a review.
  - **Assumptions**: the implementation session answers `spec_ambiguous` and `assumptions`; the assumptions are listed in the PR description, which becomes the squash commit's body, so they stay in git history for later review. Asking a person mid-run when the spec is ambiguous (a gate) waits for the planner (M6).
  - **API compatibility** (fact's own repository): a committed `api/openapi.json` snapshot and a test comparing the live schema with it. A removed or changed route or field fails; an added one fails until the snapshot is regenerated, so every API change shows as a diff of `api/openapi.json`. The same file feeds the UI's TypeScript types. Breaking compatibility is allowed only when the task explicitly asks for it, and such a change always needs a human review, whatever the project config says.
  - **Output**: a `fact:auto-merge` or `fact:needs-review` label on each PR, and a Review section in the PR description: the signals that fired, risk and door with their reasons, assumptions, security findings. `prs_ready` notifies only for `needs-review` and shows the reasons.
- **Wait for review, target (decided 27 Nov 2026)**: the wait also reads each PR's reviews: an approval on the head merges it once CI is green, and a review requesting changes starts a `review_reply` round (`converse`), as in `07-reference-workflow.md`; each PR ends on its own and the run waits for all.
- **Wait for review (M2-05, review flow since #94)**: `FactWorkflow.until_reviewed(prs, on_review)` keeps the run open until every PR is merged or closed. Each wait is a gate-like step (stage `wait_for_review`, kind `gate`): the run shows `waiting` and the wait is human wait time, but GitHub answers it, not `answer_gate`, so `fact gate` cannot. The `wait_for_review` control activity reads each open PR and its reviews every 60 s, heartbeating, and returns the first event per PR: `merged`, `closed`, `approved` (an `APPROVED` review on the PR's current head, by a person with `write`, `maintain` or `admin` permission on the repository, newer than every `CHANGES_REQUESTED` review) or `changes_requested` (a `CHANGES_REQUESTED` review by such a person that the run has not handled: the workflow passes the handled review ids and keeps them in its state). All of a run's waits share 30 days (`LONG_WAIT_RETRY` within that, so worker restarts do not exhaust retries). `Standard` merges an approved PR alone once CI is green on that head (the CI loop: a fix moves the head, so it needs a new approval), with the project's `merge_method`, whatever `auto_merge` says; a merge refused as out of date takes the base and waits for a new approval, any other refusal waits for a person. A review requesting changes runs `converse` with the `review_reply` prompt over the review and every comment since fact last pushed (or last replied); `implement` sends the run through implement, checks, agent review, learn, push to the same PR and CI again (no `classify`), `reply` changes nothing; then review is re-requested from the reviewer. A `review_reply` session that does not pass ends the run (`needs_human` when blocked). Single repository only: a session must not change a repository whose PR is already merged and answers `blocked` instead (prompt rule). The run ends `merged` when all merged, else `closed`; a failed wait (30 days, or GitHub refusing) ends the step `needs_human` and fails the run, keeping the workspace. Because the run is pinned to its worker build, a worker restarted on new code while a run waits leaves it stalled (see "Versions are pinned" above).
- **Merge**: auto-merge is a project setting, on by default (decided 26 Nov 2026, built in #14): a change set classified `auto_merge` is merged by fact through the GitHub App, with no agent, once CI is green on the exact head commit; the merge method is a project setting (`squash`, `merge` or `rebase`, default `squash`). The run ends `merged` and notifies no one. If branch protection refuses the merge, the change set falls back to `needs-review` and notifies. Built (#14, M3): `Standard` merges through `merge_change_set` (the head-SHA guard, then the project's `merge_method`, in the recorded repository order); fact's own repository is classified and merged like any other (`.fact/project.yaml`: `auto_merge: true`, with `core/migrations/**`, `api/openapi.json`, `.fact/**` and `.github/**` always routed to a review).
- **Merge queue (decided 27 Nov 2026)**: a project whose base branch uses a GitHub merge queue replaces `ChangeSet`'s `merge` slot with the library step `QueueMerge`. Once CI is green on the head, it enqueues each PR at that head (GraphQL `enqueuePullRequest` with `expectedHeadOid`, the head-SHA guard) instead of merging it, then waits, holding no compute, until GitHub merges it or drops it from the queue. The queue tests each PR on top of the base and every PR ahead of it (the `merge_group` CI event), so no run merges the base just because the base moved on. A PR dropped for a conflict or for checks failing on its merge group goes back through the base-sync and CI loop like a `dirty` PR (counted in the same cap), then is enqueued again. Several PRs are enqueued one at a time in the recorded repository order. `Standard` keeps the direct merge; fact's own workflow uses `QueueMerge` once its repository has a queue.
- **Comments**: a PR comment that mentions the factory sends a signal to the run, which stays open until its PRs merge or close. The fact GitHub App sees every comment; v0 finds them by polling with the App's token (no public URL needed), webhooks come once fact is hosted. Comments may use `/fact` or `@sofact-dev`, the App's handle.

### Review evidence and extra gates

Every PR carries the evidence a reviewer needs, built by the factory from run data rather than written by an agent, and extra gates plug in as more checks with capped fix loops.

| Item | What it is | Phase |
| --- | --- | --- |
| Scripted PR description | Title and summary written by the agent as a step output; the rest (issue, refreshed context, check results, run link, sibling PRs) filled in from run data by a template | v0 |
| Cost comment | A PR comment with time, turns, peak context, tokens (uncached input, cache read, cache write, output: each summed over every turn of the session, each billed at its own rate) and cost per step and per harness and model (marker `<!-- fact:cost -->`); `FactWorkflow.execute` posts it once, as the run's last step, on every ending that opened a PR (`merged`, `closed`, `failed`, `needs_human`, `stopped`). Only usage the harness reported is shown: an unreported cost reads `unknown` and the totals say how many are missing | v0 |
| Review checklist per project | `.fact/prompts/review.md` with the project's rules; always includes "does the change meet the issue's acceptance criteria" | v0 |
| Head-SHA guard | Before an automatic merge, confirm CI passed on exactly the commit being merged | v0 |
| Screenshots | For UI projects, a Playwright screenshot of every screen the change touches, attached to the PR and stored in object storage | Later |
| Preview link | A running preview of the change linked from the PR, using the project's preview deploy command | Later (v0 only for `incident-fix`) |
| Architecture diff | A generated before-and-after diagram of the components the change touches | Later |
| Static analysis | SonarCloud (or similar) findings read from the PR's checks, fed to a fix session, at most 3 passes | Later |

All evidence files go to object storage; the PR and the run graph link to them.
