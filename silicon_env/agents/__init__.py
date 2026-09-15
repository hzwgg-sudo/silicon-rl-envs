"""Agent implementations for silicon engineering tasks."""

from silicon_env.agents.baseline import NOOP_MAX_ACTIONS, NoopResult, run_noop_agent
from silicon_env.agents.openroad_search import (
    SEARCH_MAX_CANDIDATES,
    SEARCH_MAX_TOOL_CALLS,
    CandidateRecord,
    SearchResult,
    parse_observed_metrics,
    run_search_agent,
    search_candidates,
)

__all__ = [
    "NOOP_MAX_ACTIONS",
    "SEARCH_MAX_CANDIDATES",
    "SEARCH_MAX_TOOL_CALLS",
    "CandidateRecord",
    "NoopResult",
    "SearchResult",
    "parse_observed_metrics",
    "run_noop_agent",
    "run_search_agent",
    "search_candidates",
]
