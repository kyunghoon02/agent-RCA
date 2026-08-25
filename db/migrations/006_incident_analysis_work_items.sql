CREATE TABLE incident_analysis_work_items (
    incident_id TEXT PRIMARY KEY REFERENCES incidents (incident_id) ON DELETE CASCADE,
    context_id TEXT NOT NULL REFERENCES context_packages (context_id) ON DELETE RESTRICT,
    stage TEXT NOT NULL DEFAULT 'ANALYSIS' CHECK (stage = 'ANALYSIS'),
    state TEXT NOT NULL DEFAULT 'READY'
        CHECK (state IN ('READY', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    available_at TIMESTAMPTZ NOT NULL,
    claim_token TEXT,
    worker_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_error_code TEXT,
    CHECK (
        (state = 'READY' AND claim_token IS NULL AND worker_id IS NULL
            AND lease_expires_at IS NULL AND completed_at IS NULL)
        OR
        (state = 'RUNNING' AND claim_token IS NOT NULL AND worker_id IS NOT NULL
            AND lease_expires_at IS NOT NULL AND claimed_at IS NOT NULL
            AND completed_at IS NULL)
        OR
        (state IN ('SUCCEEDED', 'FAILED') AND claim_token IS NOT NULL
            AND worker_id IS NOT NULL AND lease_expires_at IS NULL
            AND claimed_at IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE INDEX incident_analysis_work_items_claim_idx
    ON incident_analysis_work_items (
        state, available_at, lease_expires_at, incident_id
    );

CREATE FUNCTION enqueue_incident_analysis_work_from_status()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'ANALYZING' AND OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO incident_analysis_work_items (
            incident_id, context_id, available_at
        )
        SELECT NEW.incident_id, context.context_id, NEW.updated_at
        FROM context_packages AS context
        WHERE context.incident_id = NEW.incident_id
        ORDER BY context.frozen_at DESC, context.context_id DESC
        LIMIT 1
        ON CONFLICT (incident_id) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER incidents_enqueue_analysis_work
AFTER UPDATE OF status ON incidents
FOR EACH ROW
EXECUTE FUNCTION enqueue_incident_analysis_work_from_status();

CREATE FUNCTION enqueue_incident_analysis_work_from_context()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO incident_analysis_work_items (
        incident_id, context_id, available_at
    )
    SELECT NEW.incident_id, NEW.context_id, NEW.frozen_at
    FROM incidents AS incident
    WHERE incident.incident_id = NEW.incident_id
      AND incident.status = 'ANALYZING'
    ON CONFLICT (incident_id) DO NOTHING;
    RETURN NEW;
END;
$$;

CREATE TRIGGER contexts_enqueue_analysis_work
AFTER INSERT ON context_packages
FOR EACH ROW
EXECUTE FUNCTION enqueue_incident_analysis_work_from_context();

INSERT INTO incident_analysis_work_items (
    incident_id, context_id, available_at
)
SELECT incident.incident_id, context.context_id, incident.updated_at
FROM incidents AS incident
JOIN LATERAL (
    SELECT candidate.context_id
    FROM context_packages AS candidate
    WHERE candidate.incident_id = incident.incident_id
    ORDER BY candidate.frozen_at DESC, candidate.context_id DESC
    LIMIT 1
) AS context ON TRUE
WHERE incident.status = 'ANALYZING'
ON CONFLICT (incident_id) DO NOTHING;
