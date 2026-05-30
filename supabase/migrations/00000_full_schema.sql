-- ============================================================================
-- RINKEL CALL AUDITOR — COMPLETE DATABASE SCHEMA
-- ============================================================================
-- Combined migration for one-shot deployment to Supabase SQL Editor.
--
-- Run this file in your Supabase project:
--   Dashboard → SQL Editor → New Query → Paste → Run
--
-- Tables created:
--   1. agents          — call center agents
--   2. calls           — central call records (fact table)
--   3. transcripts     — Whisper transcription output
--   4. webhook_events  — immutable webhook log
--   5. export_jobs     — JSONL export batches
--   6. audit_logs      — system-wide audit trail
--
-- Also creates:
--   - 6 enum types
--   - 30+ optimised indexes (B-tree, GIN, partial)
--   - RLS policies for service_role, anon, authenticated
--   - Auto-audit trigger on call status changes
--   - Transcript status sync trigger
--   - Dashboard views (call_statistics, agent_performance)
-- ============================================================================


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ PART 1: ENUM TYPES                                                     ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

CREATE TYPE call_direction AS ENUM (
    'inbound',
    'outbound',
    'internal'
);

CREATE TYPE call_status AS ENUM (
    'received',
    'downloading',
    'download_failed',
    'uploading',
    'upload_failed',
    'transcribing',
    'transcription_failed',
    'transcribed',
    'exporting',
    'exported',
    'audited',
    'archived'
);

CREATE TYPE transcript_status AS ENUM (
    'pending',
    'processing',
    'completed',
    'failed',
    'retrying'
);

CREATE TYPE webhook_status AS ENUM (
    'received',
    'validated',
    'processing',
    'processed',
    'failed',
    'ignored'
);

CREATE TYPE export_status AS ENUM (
    'pending',
    'running',
    'completed',
    'failed',
    'cancelled'
);

CREATE TYPE audit_action AS ENUM (
    'call.received',
    'call.status_changed',
    'call.audio_uploaded',
    'call.transcribed',
    'call.exported',
    'call.audited',
    'call.failed',
    'agent.created',
    'agent.updated',
    'agent.deactivated',
    'export.started',
    'export.completed',
    'export.failed',
    'webhook.received',
    'webhook.failed',
    'system.error'
);


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ PART 2: UTILITY FUNCTIONS                                              ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ PART 3: TABLES                                                         ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- ── 1. AGENTS ───────────────────────────────────────────────────────────────

CREATE TABLE agents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_agent_id   TEXT UNIQUE,
    display_name        TEXT NOT NULL,
    email               TEXT,
    phone_number        TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    metadata            JSONB DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER agents_updated_at
    BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE agents IS 'Call center agents who handle Rinkel calls';


-- ── 2. CALLS ────────────────────────────────────────────────────────────────

CREATE TABLE calls (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- External references
    external_call_id    TEXT NOT NULL UNIQUE,
    agent_id            UUID REFERENCES agents(id) ON DELETE SET NULL,

    -- Source & direction
    source              TEXT NOT NULL DEFAULT 'rinkel',
    direction           call_direction NOT NULL DEFAULT 'inbound',

    -- Caller / callee
    caller_number       TEXT,
    caller_name         TEXT,
    callee_number       TEXT,
    callee_name         TEXT,

    -- Timing
    started_at          TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,
    duration_seconds    INTEGER DEFAULT 0 CHECK (duration_seconds >= 0),
    ring_duration_seconds INTEGER DEFAULT 0 CHECK (ring_duration_seconds >= 0),

    -- Status
    status              call_status NOT NULL DEFAULT 'received',
    status_changed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Audio
    recording_url       TEXT,
    audio_drive_file_id TEXT,
    audio_drive_url     TEXT,
    audio_duration_seconds INTEGER,
    audio_format        TEXT DEFAULT 'wav',
    audio_size_bytes    BIGINT,

    -- Transcript
    transcript_status   transcript_status DEFAULT 'pending',

    -- Export
    last_export_id      UUID,
    exported_at         TIMESTAMPTZ,

    -- Error handling
    error_message       TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_retry_at       TIMESTAMPTZ,

    -- Metadata
    webhook_payload     JSONB DEFAULT '{}'::JSONB,
    metadata            JSONB DEFAULT '{}'::JSONB,
    tags                TEXT[] DEFAULT '{}',

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER calls_updated_at
    BEFORE UPDATE ON calls
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE calls IS 'Central call records — one row per Rinkel webhook call event';


-- ── 3. TRANSCRIPTS ──────────────────────────────────────────────────────────

CREATE TABLE transcripts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id             UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    content             TEXT NOT NULL,
    content_length      INTEGER GENERATED ALWAYS AS (LENGTH(content)) STORED,
    language            TEXT DEFAULT 'nl',
    confidence_score    NUMERIC(5, 4),
    model_name          TEXT NOT NULL DEFAULT 'base',
    model_version       TEXT,
    status              transcript_status NOT NULL DEFAULT 'pending',
    processing_time_ms  INTEGER,
    error_message       TEXT,
    segments            JSONB,
    metadata            JSONB DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER transcripts_updated_at
    BEFORE UPDATE ON transcripts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE transcripts IS 'Whisper transcription output for call audio';


-- ── 4. WEBHOOK_EVENTS ───────────────────────────────────────────────────────

CREATE TABLE webhook_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source              TEXT NOT NULL DEFAULT 'rinkel',
    event_type          TEXT NOT NULL,
    idempotency_key     TEXT UNIQUE,
    headers             JSONB DEFAULT '{}'::JSONB,
    payload             JSONB NOT NULL,
    signature           TEXT,
    status              webhook_status NOT NULL DEFAULT 'received',
    call_id             UUID REFERENCES calls(id) ON DELETE SET NULL,
    error_message       TEXT,
    processing_time_ms  INTEGER,
    ip_address          INET,
    user_agent          TEXT,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE webhook_events IS 'Immutable log of all incoming Rinkel webhooks';


-- ── 5. EXPORT_JOBS ──────────────────────────────────────────────────────────

CREATE TABLE export_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name            TEXT,
    batch_number        INTEGER,
    filter_criteria     JSONB DEFAULT '{}'::JSONB,
    call_count          INTEGER NOT NULL DEFAULT 0 CHECK (call_count >= 0),
    date_range_start    TIMESTAMPTZ,
    date_range_end      TIMESTAMPTZ,
    file_path           TEXT,
    file_size_bytes     BIGINT,
    file_checksum       TEXT,
    status              export_status NOT NULL DEFAULT 'pending',
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    processing_time_ms  INTEGER,
    error_message       TEXT,
    metadata            JSONB DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER export_jobs_updated_at
    BEFORE UPDATE ON export_jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Add deferred FK from calls → export_jobs
ALTER TABLE calls
    ADD CONSTRAINT fk_calls_last_export
    FOREIGN KEY (last_export_id) REFERENCES export_jobs(id) ON DELETE SET NULL;

COMMENT ON TABLE export_jobs IS 'JSONL export batches for Claude auditing pipeline';


-- ── 6. AUDIT_LOGS ───────────────────────────────────────────────────────────

CREATE TABLE audit_logs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action              audit_action NOT NULL,
    description         TEXT,
    agent_id            UUID REFERENCES agents(id) ON DELETE SET NULL,
    call_id             UUID REFERENCES calls(id) ON DELETE SET NULL,
    entity_type         TEXT,
    entity_id           UUID,
    old_values          JSONB,
    new_values          JSONB,
    metadata            JSONB DEFAULT '{}'::JSONB,
    source              TEXT DEFAULT 'system',
    ip_address          INET,
    user_agent          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE audit_logs IS 'Append-only audit trail for compliance and debugging';


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ PART 4: INDEXES                                                        ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- Agents
CREATE INDEX idx_agents_active ON agents (display_name) WHERE is_active = TRUE;
CREATE INDEX idx_agents_email ON agents (email) WHERE email IS NOT NULL;

-- Calls — hot-path indexes
CREATE INDEX idx_calls_status ON calls (status);
CREATE INDEX idx_calls_status_pending ON calls (status, created_at)
    WHERE status IN ('received', 'downloading', 'uploading', 'transcribing');
CREATE INDEX idx_calls_agent_id ON calls (agent_id) WHERE agent_id IS NOT NULL;
CREATE INDEX idx_calls_agent_status ON calls (agent_id, status) WHERE agent_id IS NOT NULL;
CREATE INDEX idx_calls_started_at ON calls (started_at DESC) WHERE started_at IS NOT NULL;
CREATE INDEX idx_calls_created_at ON calls (created_at DESC);
CREATE INDEX idx_calls_direction ON calls (direction);
CREATE INDEX idx_calls_caller_number ON calls (caller_number) WHERE caller_number IS NOT NULL;
CREATE INDEX idx_calls_callee_number ON calls (callee_number) WHERE callee_number IS NOT NULL;
CREATE INDEX idx_calls_transcript_status ON calls (transcript_status)
    WHERE transcript_status IN ('pending', 'processing', 'failed');
CREATE INDEX idx_calls_unexported ON calls (created_at)
    WHERE exported_at IS NULL AND status = 'transcribed';
CREATE INDEX idx_calls_source ON calls (source);
CREATE INDEX idx_calls_tags ON calls USING GIN (tags);
CREATE INDEX idx_calls_metadata ON calls USING GIN (metadata jsonb_path_ops);
CREATE INDEX idx_calls_failed ON calls (status, retry_count, last_retry_at)
    WHERE status IN ('download_failed', 'upload_failed', 'transcription_failed');

-- Transcripts
CREATE INDEX idx_transcripts_call_id ON transcripts (call_id);
CREATE INDEX idx_transcripts_status ON transcripts (status)
    WHERE status IN ('pending', 'processing', 'failed');
CREATE INDEX idx_transcripts_model ON transcripts (model_name);
CREATE INDEX idx_transcripts_language ON transcripts (language);
CREATE INDEX idx_transcripts_content_fts ON transcripts USING GIN (to_tsvector('dutch', content));

-- Webhook events
CREATE INDEX idx_webhook_events_status ON webhook_events (status);
CREATE INDEX idx_webhook_events_received_at ON webhook_events (received_at DESC);
CREATE INDEX idx_webhook_events_call_id ON webhook_events (call_id) WHERE call_id IS NOT NULL;
CREATE INDEX idx_webhook_events_event_type ON webhook_events (event_type);
CREATE INDEX idx_webhook_events_source_type ON webhook_events (source, event_type);
CREATE INDEX idx_webhook_events_failed ON webhook_events (status, received_at) WHERE status = 'failed';
CREATE INDEX idx_webhook_events_payload ON webhook_events USING GIN (payload jsonb_path_ops);

-- Export jobs
CREATE INDEX idx_export_jobs_status ON export_jobs (status);
CREATE INDEX idx_export_jobs_created_at ON export_jobs (created_at DESC);
CREATE INDEX idx_export_jobs_active ON export_jobs (status) WHERE status IN ('pending', 'running');

-- Audit logs
CREATE INDEX idx_audit_logs_action ON audit_logs (action);
CREATE INDEX idx_audit_logs_created_at ON audit_logs (created_at DESC);
CREATE INDEX idx_audit_logs_call_id ON audit_logs (call_id) WHERE call_id IS NOT NULL;
CREATE INDEX idx_audit_logs_agent_id ON audit_logs (agent_id) WHERE agent_id IS NOT NULL;
CREATE INDEX idx_audit_logs_entity ON audit_logs (entity_type, entity_id) WHERE entity_id IS NOT NULL;
CREATE INDEX idx_audit_logs_action_time ON audit_logs (action, created_at DESC);


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ PART 5: ROW-LEVEL SECURITY                                             ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

-- Service role: full access
CREATE POLICY "service_role_agents_all" ON agents FOR ALL TO service_role USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY "service_role_calls_all" ON calls FOR ALL TO service_role USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY "service_role_transcripts_all" ON transcripts FOR ALL TO service_role USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY "service_role_webhook_events_all" ON webhook_events FOR ALL TO service_role USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY "service_role_export_jobs_all" ON export_jobs FOR ALL TO service_role USING (TRUE) WITH CHECK (TRUE);
CREATE POLICY "service_role_audit_logs_all" ON audit_logs FOR ALL TO service_role USING (TRUE) WITH CHECK (TRUE);

-- Anon: only webhook insertion
CREATE POLICY "anon_webhook_insert" ON webhook_events FOR INSERT TO anon WITH CHECK (TRUE);

-- Authenticated: read-only
CREATE POLICY "authenticated_agents_select" ON agents FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY "authenticated_calls_select" ON calls FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY "authenticated_transcripts_select" ON transcripts FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY "authenticated_export_jobs_select" ON export_jobs FOR SELECT TO authenticated USING (TRUE);
CREATE POLICY "authenticated_audit_logs_select" ON audit_logs FOR SELECT TO authenticated USING (TRUE);


-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ PART 6: TRIGGERS & VIEWS                                               ║
-- ╚══════════════════════════════════════════════════════════════════════════╝

-- Auto-audit: log every call status change
CREATE OR REPLACE FUNCTION log_call_status_change()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO audit_logs (action, description, agent_id, call_id, entity_type, entity_id, old_values, new_values, source)
        VALUES (
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
        NEW.status_changed_at = NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER calls_audit_status_change
    BEFORE UPDATE ON calls
    FOR EACH ROW EXECUTE FUNCTION log_call_status_change();

-- Sync transcript status to calls table
CREATE OR REPLACE FUNCTION sync_transcript_status()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE calls SET transcript_status = NEW.status WHERE id = NEW.call_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER transcripts_sync_status
    AFTER INSERT OR UPDATE OF status ON transcripts
    FOR EACH ROW EXECUTE FUNCTION sync_transcript_status();

-- Dashboard views
CREATE OR REPLACE VIEW call_statistics AS
SELECT
    DATE_TRUNC('day', c.created_at) AS day,
    c.direction,
    c.status,
    COUNT(*) AS call_count,
    AVG(c.duration_seconds) AS avg_duration_seconds,
    SUM(c.duration_seconds) AS total_duration_seconds,
    COUNT(*) FILTER (WHERE c.transcript_status = 'completed') AS transcribed_count,
    COUNT(*) FILTER (WHERE c.exported_at IS NOT NULL) AS exported_count
FROM calls c
GROUP BY DATE_TRUNC('day', c.created_at), c.direction, c.status;

CREATE OR REPLACE VIEW agent_performance AS
SELECT
    a.id AS agent_id,
    a.display_name,
    a.is_active,
    COUNT(c.id) AS total_calls,
    COUNT(c.id) FILTER (WHERE c.direction = 'inbound') AS inbound_calls,
    COUNT(c.id) FILTER (WHERE c.direction = 'outbound') AS outbound_calls,
    ROUND(AVG(c.duration_seconds), 1) AS avg_call_duration,
    MAX(c.created_at) AS last_call_at
FROM agents a
LEFT JOIN calls c ON c.agent_id = a.id
GROUP BY a.id, a.display_name, a.is_active;
