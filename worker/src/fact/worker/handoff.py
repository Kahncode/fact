"""The handoff note a dropped run leaves for the next run of its task, as markdown."""

from fact.core.activity_contract import HandoffInput

HANDOFF_HEADING = "## Handoff from a dropped run"


def render_handoff(spec: HandoffInput) -> str:
    """Render the note: why the run was dropped, the last stage it reached, its PRs and each agent step's summary."""
    stage = f"`{spec.stage_id}` (attempt {spec.attempt})" if spec.stage_id else "none: it had not started a step"
    lines = [
        HANDOFF_HEADING,
        "",
        f"A previous run of {spec.task.key} (`{spec.run_id}`) was dropped so the task could restart on newer code.",
        (
            "Its branch is kept: your workspace starts from it, with every commit that run pushed. Check `git log`"
            " before you start, keep the work that is good, and finish the task."
        ),
        "",
        f"- Reason: {spec.reason or 'none given'}",
        f"- Last stage reached: {stage}",
    ]
    if spec.prs:
        lines.append("- Open PRs, reused by this run: " + ", ".join(pr.url for pr in spec.prs))
    lines.append("")
    if not spec.steps:
        lines.append("It ran no agent session.")
    for step in spec.steps:
        lines.extend([f"### {step.stage_id} (attempt {step.attempt}): {step.outcome}", "", step.summary.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"
