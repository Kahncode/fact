# Future ideas

Noted from a webinar on 24 Nov 2026; not in v0.

- **Dolt for context**: keep agent context in a Dolt database (version-controlled SQL) instead of markdown files, so context can be queried, branched and diffed like code.
- **Code discovery tool**: map an app's domains and features visually, built from a mix of static analysis tools and LLMs, and feed that map to agents as context.
- **Bug and feedback sources**: connect bug databases and user feedback databases as inputs, beside GitHub Issues and Linear.
- **More review evidence and gates**: screenshots of affected screens, preview links for every PR, architecture diffs, and SonarCloud or similar static analysis with a capped fix loop (see Review evidence and extra gates).
- **Ideas from Warp Factories**: an Autonomy metric (share of merged PRs with no human push) beside human touches; cost per PR split into compute and inference; MCP tools so a local Claude Code session can hand work to fact and pull it back; LLM-judge scorers and replayed benchmarks to compare models and prompts; validating changes to `.fact/` on the PR that makes them.

## Generated logos (M8 stretch, very low priority)

Decided 27 Nov 2026. The app and the landing page show a logo that changes on a schedule, in the spirit of Google Doodles; the favicon, `web/public/brand/avatar.png` and the GitHub App avatar stay the fixed logomark.

- **Generation:** a recurring job asks a real image-generation model for logo variants. Its cron timing and the number of days it generates per batch are configurable (one a day, or a week at a time).
- **Brand rules:** every variant follows the brand book (M8): a portrait in side profile facing right, pencil on warm paper, the brand palette (sage accent `#748D78`, see `11-ui-and-notifications.md`), generous negative space, no text. The subject varies: a human, a cyborg, an animal, a robot or an object. The starting prompt is the one that produced the current logomark (in the issue), with its accent changed to sage.
- **Themes:** a curated calendar of dates to theme, plus a Claude step with web search that proposes themes for the coming days (holidays, science and tech anniversaries, seasons). Excluded: tragedies, elections, religious conflict, real living people. Unthemed days get a random allowed subject. A themed logo carries a one-line caption, shown on hover.
- **Check, no human in the loop:** a Claude vision step reads each image with the brand book and judges it safe (nothing offensive, sexualised or political, no stereotypes) and on brand (faces right, portrait, palette, no text). A failed image is regenerated up to 3 times; then the day falls back to the canonical logomark.
- **Delivery:** images go to object storage and the app picks the current day's logo, falling back to the logomark.
- **Open until specced:** the image provider (evaluate when specced), storage and serving, and where the job runs (a cron in the deployed backend, M5; the `fact-scheduler` decision in `03-temporal.md` is unchanged until then). Needs the brand book first; ordering is settled in M6 planning.
