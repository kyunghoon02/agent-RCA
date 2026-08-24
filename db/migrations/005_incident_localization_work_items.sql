CREATE TABLE incident_localization_work_items (
    incident_id TEXT PRIMARY KEY REFERENCES incidents (incident_id) ON DELETE CASCADE,
    stage TEXT NOT NULL DEFAULT 'LOCALIZATION' CHECK (stage = 'LOCALIZATION'),
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

CREATE INDEX incident_localization_work_items_claim_idx
    ON incident_localization_work_items (
        state, available_at, lease_expires_at, incident_id
    );

CREATE FUNCTION enqueue_incident_localization_work()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'LOCALIZING' AND OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO incident_localization_work_items (incident_id, available_at)
        VALUES (NEW.incident_id, NEW.updated_at)
        ON CONFLICT (incident_id) DO NOTHING;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER incidents_enqueue_localization_work
AFTER UPDATE OF status ON incidents
FOR EACH ROW
EXECUTE FUNCTION enqueue_incident_localization_work();

INSERT INTO incident_localization_work_items (incident_id, available_at)
SELECT incident_id, updated_at
FROM incidents
WHERE status = 'LOCALIZING'
ON CONFLICT (incident_id) DO NOTHING;
