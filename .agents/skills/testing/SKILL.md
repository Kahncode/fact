---
name: testing
description: The rules for which tests to write and how. Use before writing, changing or deleting a test, and when reviewing a change's tests.
scope: project
---

# Testing

A test is code we pay for on every run, every refactor and every review. It earns its place by catching a mistake that a reviewer reading the diff would plausibly miss, and that would otherwise reach users or later code. Write the fewest tests that pin the behavior; when in doubt, leave it out.

## What earns a test

1. **Spec rules**: behavior `docs/spec/` or the task states as a rule (the failure cap counts back to the latest manual run; an unknown cost is NULL, never 0).
2. **Published contracts**: the API and its OpenAPI snapshot, Temporal handler names and payloads, the database schema and migrations, task-source write-backs, CLI exit codes and machine-readable (`--json`) output.
3. **The golden path of each feature, once**, through its public entry point: the route, activity, workflow or command a caller uses.
4. **Edge cases of logic that branches**: empty, boundary, limit reached, malformed input, each error path the code handles on purpose.
5. **Regressions**: a bug that shipped gets a test that fails without the fix.
6. **Safety invariants**: secrets kept out of logs, argv and payloads; path traversal refused; workspace isolation; workflow determinism under replay.

An acceptance criterion gets a test only when it falls in this list. Otherwise its evidence is a `file:line` or a command and its output: "`fact github labels sync` creates the four `priority:*` labels" is covered by the golden-path test of `labels sync` and `labels.py:<line>`, not by a test that the list holds four labels.

## What a reviewer checks by reading

These cost more to keep than they catch; the diff shows them:

- Constants, defaults, literal mappings and config values, ours or shipped (`fact.yaml`, `.fact/*`). Committed config gets one test that every file validates, never a test of its values.
- Constructing a Pydantic model, dataclass or enum; field names; getters; one-line glue.
- What a framework already guarantees: Pydantic validation, FastAPI 422s, SQLAlchemy, Typer parsing, httpx, Temporal itself.
- Wording of prompts, messages, comments and CLI tables. Assert the key values a reader relies on (the id, URL, cost, status). Text that is itself the product (PR body, cost comment, handoff note) gets one golden test per renderer.
- Private helpers already exercised through the public path.
- Exact call order and exact mock arguments. Assert outcomes and the invariants the spec states ("`learn` runs before `mark_done`"), never the full sequence of calls.
- Log lines, unless the log is the behavior (a token is never logged).

## Shape

- **Wide over many**: wiring, routing and glue are tested through their entry point, in the golden-path test. A unit with real logic (parsing, ordering, state machines, limit counting, time math) gets its own focused tests.
- **One place per behavior**: a behavior shared by many callers (unknown run → non-retryable, API down → exit 1, a best-effort step failing leaves the run going) is tested once where it is implemented. Variants of one behavior are one `pytest.mark.parametrize` table, not near-copies.
- **Real over mocked**: real SQLite with migrations, real git repos, Temporal's time-skipping environment, `httpx.MockTransport` fakes that keep state. A mock stands in for what is slow or outside (GitHub, Claude), and the assertion checks what our code did with the fake's answer, never the answer echoed back.
- **Assert what matters**: as many assertions as the behavior needs, often one. Each assertion is one a wrong implementation would fail.
- **Adjust before adding**: when a change alters behavior, update or delete the tests that pinned the old behavior before writing new ones. A test that no longer earns its place by the list above is deleted.

## Deterministic

A test gives the same answer on every run, on every OS, under load:

- Time is controlled: Temporal's time-skipping environment, an injected clock, or an `asyncio.Event` or future the test sets. Real `sleep` and polling loops cost wall time on every CI run; wait on the event that marks the condition instead.
- Concurrency is forced: a test of an interleaving drives it with events or barriers, so it holds whatever the scheduler does. A race the test cannot force is reviewed, not tested.
- Each wait that could hang (a subprocess, a workflow result, a queue) has its own short timeout, so a hang fails fast with its cause; pytest-timeout's 60 s is the last resort.
- Network is localhost fakes only, on port 0 (the OS picks a free port). Paths come from `tmp_path`.
- Inputs are fixed: no `now()`, random values or dict-order luck in assertions.
- A test on a shared Temporal server reads time from the server (`await env.get_current_time()`); earlier tests have skipped its clock ahead.
- A test that fails once without a code change is flaky: fix its cause or delete it. Never rerun until green.

## Fast

The suite runs on every check loop and every CI push. Guidelines, not limits:

- A unit test runs well under 100 ms. One over about 1 s is a golden path, a regression or a safety test, and earns its time.
- Expensive setup (a Temporal environment, a template git repo, a migrated database) is built once per module and copied or reset per test, as long as tests stay independent. A Temporal test server is shared per module, never per run.
- A test of a real timeout or kill uses the shortest duration that still proves it.
