CREATE TABLE incidents (
    incident_id TEXT PRIMARY KEY,
    deduplication_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    document JSONB NOT NULL
);

CREATE INDEX incidents_status_updated_idx
    ON incidents (status, updated_at DESC);

CREATE TABLE incident_audit_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    details JSONB NOT NULL
);

CREATE INDEX incident_audit_events_incident_time_idx
    ON incident_audit_events (incident_id, occurred_at, event_id);

CREATE TABLE evidence_items (
    evidence_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    document JSONB NOT NULL
);

CREATE INDEX evidence_items_incident_time_idx
    ON evidence_items (incident_id, observed_at, evidence_id);

CREATE TABLE context_packages (
    context_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
    frozen_at TIMESTAMPTZ NOT NULL,
    document JSONB NOT NULL
);

CREATE INDEX context_packages_incident_time_idx
    ON context_packages (incident_id, frozen_at DESC, context_id);

CREATE TABLE rca_reports (
    report_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents (incident_id) ON DELETE CASCADE,
    context_id TEXT NOT NULL REFERENCES context_packages (context_id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    document JSONB NOT NULL,
    markdown TEXT NOT NULL CHECK (length(btrim(markdown)) > 0)
);

CREATE INDEX rca_reports_incident_time_idx
    ON rca_reports (incident_id, generated_at DESC, report_id);
