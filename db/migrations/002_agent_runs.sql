CREATE TABLE agent_runs (
    agent_run_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    context_id TEXT NOT NULL REFERENCES context_packages(context_id),
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    document JSONB NOT NULL
);

CREATE INDEX agent_runs_incident_started_idx
    ON agent_runs (incident_id, started_at DESC);
