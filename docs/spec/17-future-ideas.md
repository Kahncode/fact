# Future ideas

Noted from a webinar on 24 Nov 2026; not in v0.

- **Dolt for context**: keep agent context in a Dolt database (version-controlled SQL) instead of markdown files, so context can be queried, branched and diffed like code.
- **Code discovery tool**: map an app's domains and features visually, built from a mix of static analysis tools and LLMs, and feed that map to agents as context.
- **Bug and feedback sources**: connect bug databases and user feedback databases as inputs, beside GitHub Issues and Linear.
- **More review evidence and gates**: screenshots of affected screens, preview links for every PR, architecture diffs, and SonarCloud or similar static analysis with a capped fix loop (see Review evidence and extra gates).
- **Ideas from Warp Factories**: an Autonomy metric (share of merged PRs with no human push) beside human touches; cost per PR split into compute and inference; MCP tools so a local Claude Code session can hand work to fact and pull it back; LLM-judge scorers and replayed benchmarks to compare models and prompts; validating changes to `.fact/` on the PR that makes them.
