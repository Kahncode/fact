# Domain docs

How the engineering skills consume fact's domain documentation. fact is single-context but does not use the stock `GLOSSARY.md` + `docs/adr/` layout; it uses the files below. Don't create `GLOSSARY.md` or `docs/adr/`.

## Before exploring, read these

- **Glossary**: `docs/spec/05-domain-model.md` (the **Terms** paragraph and the entity table). Label and task-source terms are in `docs/spec/12-integrations.md`.
- **Design decisions** (the ADR log for design): `docs/spec/16-decisions.md`.
- **Implementation decisions**: `docs/decisions.md` (full, read-only) continued in `docs/decisions-2.md`, append-only, one `### [YYYY-MM-DD HH:MM] Title` entry with **What:** and **Why:**.
- **Spec index**: `docs/spec/README.md`; read only the sections your work touches.

## Writing

`docs/spec/` is the design. Planning (grilling, `/grill-with-docs`, `/domain-modeling`) keeps it current as each decision is made, not at the end:

- A new or sharpened domain term goes into `docs/spec/05-domain-model.md` (the Terms paragraph or the entity table).
- A design decision goes into `docs/spec/16-decisions.md` (the Decided table or the "Also decided" list; move items out of "Still open" once resolved), with the details in the section it changes.
- Every other design change goes into the `docs/spec/` section it affects (`docs/spec/README.md` lists them). Mark what is decided but not built yet, as the sections already do with "today" notes.

**Branch for planning edits.** Edit the docs on whatever branch is checked out, unless it is `main`. On `main`, create one `docs/<topic>` branch at the first edit and keep using it for the rest of the session; never create a branch per document or per decision.

Implementation decisions are not planning output: `docs/decisions.md` and `docs/decisions-2.md` record why the code was built the way it was, and only implementers write them. Append to `docs/decisions-2.md` in its existing format (when it reaches 300 lines, start `docs/decisions-3.md`); never edit or reorder past entries. Grilling never writes there.

## Use the glossary's vocabulary

When your output names a domain concept (task title, refactor proposal, test name), use the term as `05-domain-model.md` defines it: project vs repository, run, step, workspace, change set, harness. If a concept isn't there, either you're inventing language (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag decision conflicts

If your output contradicts an entry in `16-decisions.md` or the `docs/decisions*.md` files, say so explicitly rather than silently overriding:

> _Contradicts "Auto-merge: none in v0" (16-decisions.md), but worth reopening because…_
