---
id: M1-08
title: "Harness contract and Claude Code adapter"
type: feature
complexity: standard
status: planned
depends_on: [M1-01]
human_actions: 
  - "Claude Code CLI installed and logged in on this machine (needed to record the test fixture)"
---

## Goal

fact can run Claude Code headless on a prompt in a folder and get back what the harness contract promises: session id, final message, exit status, tokens, cost in USD and the full transcript. Later harnesses implement the same contract.

## Read first

- `CLAUDE.md`
- `docs/spec/09-runners-and-harnesses.md` (Harness contract, Choosing a harness and model)
- `core/src/fact/core/types.py`
- `claude --help` and the headless/stream-json section of the Claude Code docs

## Do

- `worker/src/fact/worker/harnesses/base.py`:
  - `HarnessRequest`: `prompt`, `cwd`, `model`, `allowed_tools`, `max_turns`, `resume_session_id`, `env`;
  - `HarnessResult`: `session_id`, `final_message`, `is_error`, `exit_code`, `stop_reason`, `num_turns`, and usage (tokens and cost);
  - a `Harness` protocol with `run(request, transcript_path, on_message)`.
- `worker/src/fact/worker/harnesses/claude_code.py`:
  - resolve the binary with `shutil.which("claude")` (on Windows it may be `claude.cmd`);
  - run `claude -p --output-format stream-json --verbose --model <m> --max-turns <n>`, plus `--allowedTools` and `--resume` when given;
  - send the prompt on stdin, not argv, because Windows limits command-line length;
  - write every stream line to the transcript file;
  - call `on_message` for progress;
  - read `session_id`, cost, usage, `num_turns`, `is_error` and the stop reason (max turns included) from the final `result` message;
  - on cancellation, kill the process tree.
- Record a real fixture by running Claude Code once on a trivial prompt, save it as `worker/tests/fixtures/claude_stream_ok.jsonl`, plus a hand-trimmed error case. Parsing is tested against these fixtures, with no network in tests. Check the field names against the fixture, not from memory.

## Out of scope

- Routing, prompts and the Temporal activity (M1-09).
- Codex, OpenCode and Pi adapters (later).
- The Claude Agent SDK plugin (backlog).

## Acceptance criteria

- [ ] `uv run pytest worker` passes using the recorded fixtures.
- [ ] A manual call (documented in `worker/README.md`) runs Claude Code on "reply with OK" in a temporary folder and prints cost and session id.
