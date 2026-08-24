CREATE TABLE stategraph_observation_cycles (
    cycle_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    evidence_scope_id TEXT NOT NULL,
    cluster_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('STAGED', 'APPLIED')),
    observed_at TIMESTAMPTZ NOT NULL,
    staged_at TIMESTAMPTZ NOT NULL,
    applied_at TIMESTAMPTZ,
    evidence_count INTEGER NOT NULL CHECK (evidence_count > 0),
    result JSONB,
    document JSONB NOT NULL,
    CHECK (
        (status = 'STAGED' AND applied_at IS NULL AND result IS NULL)
        OR
        (status = 'APPLIED' AND applied_at IS NOT NULL AND result IS NOT NULL)
    )
);

CREATE INDEX stategraph_observation_cycles_status_time_idx
    ON stategraph_observation_cycles (status, applied_at, staged_at, cycle_id);

CREATE TABLE stategraph_observation_evidence (
    evidence_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES stategraph_observation_cycles (cycle_id)
        ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    document JSONB NOT NULL
);

CREATE INDEX stategraph_observation_evidence_cycle_time_idx
    ON stategraph_observation_evidence (cycle_id, observed_at, evidence_id);
