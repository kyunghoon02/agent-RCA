"""Shared observation vocabulary; none of these signals is an RCA cause ID."""

LEGACY_FEATURE_SET = "hubble-network-flow-summary-v1"
FEATURE_SET = "hubble-network-flow-summary-v2"
VERDICTS = frozenset(
    {
        "FORWARDED",
        "DROPPED",
        "AUDIT",
        "REDIRECTED",
        "ERROR",
        "TRACED",
        "TRANSLATED",
        "UNKNOWN",
    }
)
PROTOCOLS = frozenset({"TCP", "UDP", "ICMPv4", "ICMPv6", "SCTP", "UNKNOWN"})
# Cilium distinguishes a missing allow rule from an explicit deny rule.
POLICY_DROP_REASONS = frozenset({"POLICY_DENIED", "POLICY_DENY"})
OBSERVATION_GAPS = frozenset(
    {
        "FLOW_EVENTS_LOST",
        "RELAY_NODE_UNAVAILABLE",
        "RELAY_NODE_ERROR",
        "RELAY_NODE_GONE",
        "RELAY_NODE_STATE_UNKNOWN",
        "CLI_DIAGNOSTIC",
        "CLI_RELAY_VERSION_MISMATCH",
        "RELAY_QUERY_UNAVAILABLE",
        "QUERY_BUDGET_EXHAUSTED",
    }
)


def flow_signal(flow_count: int, dropped: int, policy_denied: int) -> str:
    if policy_denied:
        return "POLICY_DENIAL_OBSERVED"
    if dropped:
        return "DROPS_OBSERVED"
    return "NO_DROPS_OBSERVED" if flow_count else "NO_FLOW_DATA"
