---
name: test
description: Write or trim tests for a file, function or the recent changes by the project's testing rules, through the test-writer agent.
scope: project
---

# Tests

Delegate to the `test-writer` agent via the Agent tool. Pass it the target from `$ARGUMENTS` (a file, a function, a diff range; the uncommitted changes when none is given) and any instruction that came with it, such as "trim" or "regression for #42".

The agent applies the `testing` skill's rules: it adds only tests that earn their place and removes the ones that don't. Relay its report: tests added, changed and deleted, each with its reason.
