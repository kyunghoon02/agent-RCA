# Incident Investigation Boundaries

This tool guide defines the safe investigation boundary for generic web services and
Kubernetes workloads.

## Required inputs

- Use the frozen Incident time window and localized Entity set.
- Read only allowlisted telemetry and resource APIs.
- Treat missing data, timeout, permission denial, and an empty result as different
  outcomes.
- Preserve the Evidence ID and source provenance for every factual claim.

## Decision boundary

Architecture, runbooks, service catalogs, SLOs, and tool guides are investigation
references. They can suggest the next read-only query, but they do not prove the
current Incident's root cause. A conclusive claim requires current runtime Evidence.

When the localized Context is incomplete or conflicting, expand only through the
approved StateGraph scope and stop with `ABSTAIN` when the budget is exhausted.

## Data handling

Do not retrieve evaluation answers, fault injection settings, credentials, Secret
values, raw out-of-scope telemetry, or unreviewed Agent output. Do not copy an
Operational Knowledge reference into an Evidence item.
