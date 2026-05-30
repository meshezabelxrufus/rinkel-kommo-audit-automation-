-- ============================================================================
-- Migration 00005: Database functions and triggers
-- ============================================================================
-- Production helper functions for:
--   1. Automatic audit logging on call status changes
--   2. Automatic transcript_status sync on calls table
--   3. Call statistics view
-- ============================================================================

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- AUTO AUDIT: Log every call status change to audit_logs
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION log_call_status_change()
RETURNS TRIGGER AS $$
BEGIN
    -- Only fire when status actually changes
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO audit_logs (
            action,
            description,
            agent_id,
            call_id,
            entity_type,
            entity_id,
            old_values,
            new_values,
            source
        ) VALUES (
            'call.status_changed',
            FORMAT('Call %s status: %s → %s', NEW.external_call_id, OLD.status, NEW.status),
            NEW.agent_id,
            NEW.id,
            'call',
            NEW.id,
            jsonb_build_object('status', OLD.status::TEXT),
            jsonb_build_object('status', NEW.status::TEXT),
            'system'
        );

        -- Also update status_changed_at
        NEW.status_changed_at = NOW();
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER calls_audit_status_change
    BEFORE UPDATE ON calls
    FOR EACH ROW EXECUTE FUNCTION log_call_status_change();


-- ────────────────────────────────────────────────────────────────────────────
-- SYNC: Update calls.transcript_status when a transcript is inserted/updated
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION sync_transcript_status()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE calls
    SET transcript_status = NEW.status
    WHERE id = NEW.call_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER transcripts_sync_status
    AFTER INSERT OR UPDATE OF status ON transcripts
    FOR EACH ROW EXECUTE FUNCTION sync_transcript_status();


-- ────────────────────────────────────────────────────────────────────────────
-- VIEW: Call statistics for dashboards
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW call_statistics AS
SELECT
    DATE_TRUNC('day', c.created_at) AS day,
    c.direction,
    c.status,
    COUNT(*)                        AS call_count,
    AVG(c.duration_seconds)         AS avg_duration_seconds,
    SUM(c.duration_seconds)         AS total_duration_seconds,
    COUNT(*) FILTER (WHERE c.transcript_status = 'completed')
                                    AS transcribed_count,
    COUNT(*) FILTER (WHERE c.exported_at IS NOT NULL)
                                    AS exported_count
FROM calls c
GROUP BY
    DATE_TRUNC('day', c.created_at),
    c.direction,
    c.status;

COMMENT ON VIEW call_statistics IS 'Daily call statistics aggregated by direction and status';


-- ────────────────────────────────────────────────────────────────────────────
-- VIEW: Agent performance summary
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW agent_performance AS
SELECT
    a.id                            AS agent_id,
    a.display_name,
    a.is_active,
    COUNT(c.id)                     AS total_calls,
    COUNT(c.id) FILTER (WHERE c.direction = 'inbound')
                                    AS inbound_calls,
    COUNT(c.id) FILTER (WHERE c.direction = 'outbound')
                                    AS outbound_calls,
    ROUND(AVG(c.duration_seconds), 1)
                                    AS avg_call_duration,
    MAX(c.created_at)               AS last_call_at
FROM agents a
LEFT JOIN calls c ON c.agent_id = a.id
GROUP BY a.id, a.display_name, a.is_active;

COMMENT ON VIEW agent_performance IS 'Per-agent call volume and duration metrics';

COMMIT;
