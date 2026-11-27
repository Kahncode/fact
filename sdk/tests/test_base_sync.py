"""`conflict_context`: what a `resolve_conflicts` session is told about the merge it must conclude."""

from fact.core.enums import StepOutcome
from fact.core.types import StepResult
from fact.sdk.base_sync import conflict_context


def test_lists_each_repository_s_unmerged_paths() -> None:
    conflicts = {"acme/web": ["src/app.ts"], "acme/api": ["docs/ARCHITECTURE.md", "AGENTS.md"]}
    result = StepResult(outcome=StepOutcome.FAILED_CHECKS, outputs={"merged": [], "conflicts": conflicts})

    text = conflict_context(result)

    assert text.index("### acme/api") < text.index("### acme/web")
    assert "- `docs/ARCHITECTURE.md`\n- `AGENTS.md`" in text
    assert "- `src/app.ts`" in text


def test_without_paths_it_falls_back_to_the_summary() -> None:
    result = StepResult(outcome=StepOutcome.FAILED_CHECKS, summary="merge failed", outputs={})
    assert conflict_context(result) == "merge failed"
