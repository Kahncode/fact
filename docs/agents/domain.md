# Domain docs

How the engineering skills consume fact's domain documentation. fact is single-context but does not use the stock `GLOSSARY.md` + `docs/adr/` layout; it uses the files below. Don't create `GLOSSARY.md` or `docs/adr/`.

## Before exploring, read these

- **Glossary**: `docs/spec/05-domain-model.md` (the **Terms** paragraph and the entity table). Label and task-source terms are in `docs/spec/12-integrations.md`.
- **Design decisions** (the ADR log for design): `docs/spec/16-decisions.md`.
- **Implementation decisions**: `docs/decisions.md`, append-only, one `### [YYYY-MM-DD HH:MM] Title` entry with **What:** and **Why:**.
- **Spec index**: `docs/spec/README.md`; read only the sections your work touches.

## Writing

- A new or sharpened domain term goes into `docs/spec/05-domain-model.md` (the Terms paragraph or the entity table).
- A design decision goes into `docs/spec/16-decisions.md` (the Decided table or the "Also decided" list; move items out of "Still open" once resolved).
- An implementation decision is appended to `docs/decisions.md` in its existing format. Never edit or reorder past entries.

## Use the glossary's vocabulary

When your output names a domain concept (task title, refactor proposal, test name), use the term as `05-domain-model.md` defines it: project vs repository, run, step, workspace, change set, harness. If a concept isn't there, either you're inventing language (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag decision conflicts

If your output contradicts an entry in `16-decisions.md` or `docs/decisions.md`, say so explicitly rather than silently overriding:

> _Contradicts "Auto-merge: none in v0" (16-decisions.md), but worth reopening because…_
