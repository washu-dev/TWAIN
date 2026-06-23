"""Explainability for the candidate ranking (Story 4.2).

Turns the numeric output of `scorers.rank_candidates` into human-readable
rationale so a researcher can see *why* a tool was ranked where it was and why
the top candidate beat the runner-up.
"""

from typing import List, Optional

from method_discovery.scorers import WEIGHTS, ScoredCandidate

# Order components by their weight (most influential first) for display.
_DISPLAY_ORDER = sorted(WEIGHTS, key=lambda k: WEIGHTS[k], reverse=True)


def component_breakdown(candidate: ScoredCandidate) -> str:
    """One-line component breakdown, e.g. 'Relevance 0.80 | Maturity 0.70 | ...'."""
    parts = candidate.components.as_dict()
    return " | ".join(f"{name.capitalize()} {parts[name]:.2f}" for name in _DISPLAY_ORDER)


def _largest_gap_component(top: ScoredCandidate, runner_up: ScoredCandidate) -> Optional[str]:
    """Return the component that contributed most to top's lead (weighted), or None."""
    top_parts = top.components.as_dict()
    ru_parts = runner_up.components.as_dict()
    best_name = None
    best_delta = 0.0
    for name in WEIGHTS:
        delta = WEIGHTS[name] * (top_parts[name] - ru_parts[name])
        if delta > best_delta:
            best_delta = delta
            best_name = name
    return best_name


def compare_top_two(ranked: List[ScoredCandidate]) -> str:
    """Explain why #1 beat #2, citing the most influential component."""
    if len(ranked) < 2:
        if len(ranked) == 1:
            return f"{ranked[0].entry.name} is the only candidate."
        return "No candidates to compare."

    top, runner_up = ranked[0], ranked[1]
    margin = top.composite - runner_up.composite
    driver = _largest_gap_component(top, runner_up)

    if driver is None or margin <= 0:
        return (
            f"{top.entry.name} and {runner_up.entry.name} are effectively tied "
            f"(composite {top.composite:.3f} vs {runner_up.composite:.3f}); "
            f"{top.entry.name} wins the tie-break by id."
        )

    top_parts = top.components.as_dict()
    ru_parts = runner_up.components.as_dict()
    comp_delta = top_parts[driver] - ru_parts[driver]
    return (
        f"{top.entry.name} beats {runner_up.entry.name} by {margin:.3f} composite "
        f"({top.composite:.3f} vs {runner_up.composite:.3f}), driven mainly by "
        f"{driver} ({top_parts[driver]:.2f} vs {ru_parts[driver]:.2f}, "
        f"+{comp_delta:.2f} before weighting)."
    )


def explain_candidate(candidate: ScoredCandidate) -> str:
    """Full single-candidate explanation line."""
    return (
        f"#{candidate.rank} {candidate.entry.name} "
        f"(composite {candidate.composite:.3f}): {component_breakdown(candidate)}"
    )


def explain_ranking(ranked: List[ScoredCandidate]) -> str:
    """Multi-line report: each candidate's breakdown plus the top-two comparison."""
    lines = [explain_candidate(c) for c in ranked]
    lines.append("")
    lines.append(compare_top_two(ranked))
    return "\n".join(lines)
