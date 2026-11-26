# Issue tracker: task files in `tasks/`

Until fact reads GitHub Issues (M2-12 moves the remaining files to issues), every ticket is a markdown task file. `tasks/README.md` is the authority on the format, fields and how to split work; read it before writing a ticket.

## Conventions

- **One ticket = one task file**: `tasks/<milestone>/<NN>-<slug>.md`, with YAML front matter (`id`, `title`, `type`, `complexity`, `status`, `depends_on`, `human_actions`) and the sections `Goal`, `Read first`, `Do`, `Out of scope`, `Acceptance criteria`.
- **Ids** are `<milestone>-<NN>`, unique and never reused. Finished task files are deleted, so check `git log --name-only -- tasks/` before taking a number.
- **New tickets are `status: planned`.** Only the person moves a task to `ready`.
- **Blocking** is `depends_on: [<id>, ...]`. A missing file counts as done under the rules in `tasks/README.md`.
- **Specs**: design lives in `docs/spec/` (one file per section). A small ticket's spec is its own task file; a larger plan updates the relevant spec section and splits into task files.
- **Ideas not yet planned** go in `tasks/backlog.md` as a `##` section.
- **Comments / conversation**: there is no comment thread. Record open questions in the task body under `## Open questions` and resolve them by editing the task.
- **Done**: the task file is deleted (`git rm`) in the commit that finishes it; never set `status: done` by hand.
- CI validates the folder (`uv run pytest` runs the task validator), so keep front matter valid.
- The project's own `plan-tasks` skill plans in this format; prefer it, or follow it, when breaking work into tasks.

## When a skill says "publish to the issue tracker"

Create a task file in the right `tasks/<milestone>/` folder with `status: planned`. Ask which milestone if it is unclear.

## When a skill says "fetch the relevant ticket"

Read the task file. The person usually gives the id (`M2-07`) or the path; glob `tasks/**/*.md` and match the `id` field.

## Wayfinding operations

Used by `/wayfinder`. Explorations are not tasks, so they live outside the milestone folders (the validator reads only those).

- **Map**: `tasks/explore/<effort>/map.md` (Notes / Decisions-so-far / Fog).
- **Child ticket**: `tasks/explore/<effort>/<NN>-<slug>.md`, with `Type:` (`research`/`prototype`/`grilling`/`task`) and `Status:` (`open`/`claimed`/`resolved`) lines at the top.
- **Blocking**: a `Blocked by: NN, NN` line; unblocked when every listed file is `resolved`.
- **Frontier**: open, unblocked, unclaimed files; lowest number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append `## Answer`, set `Status: resolved`, add a gist + link to the map's Decisions-so-far. Outcomes that become real work become task files.

## After M2: GitHub Issues

Once tasks move to issues in `Kahncode/fact`, use `gh` (`gh issue create/view/list/edit/comment/close`) and the labels in `docs/agents/triage-labels.md`. New issues get `fact:planned` plus one `type:*` and one `complexity:*` label. Update this file at that point.
