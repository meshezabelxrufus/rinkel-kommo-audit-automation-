-- ============================================================================
-- Migration 00002: Create core tables
-- ============================================================================
-- Table creation order respects foreign key dependencies:
--   1. agents         (no FK deps)
--   2. calls          (FK → agents)
--   3. transcripts    (FK → calls)
--   4. webhook_events (FK → calls, nullable)
--   5. export_jobs    (no FK deps)
--   6. audit_logs     (FK → agents, calls — both nullable)
-- ============================================================================

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- UTILITY: auto-update updated_at trigger function
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- 1. AGENTS
-- ════════════════════════════════════════════════════════════════════════════
-- Agents who handle calls. Each call is associated with one agent.
-- external_agent_id allows mapping to Rinkel's agent identifiers.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE agents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Identity
    external_agent_id   TEXT UNIQUE,                -- Rinkel's agent ID
    display_name        TEXT NOT NULL,
    email               TEXT,
    phone_number        TEXT,

    -- Status
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,

    -- Metadata
    metadata            JSONB DEFAULT '{}'::JSONB,  -- extensible fields

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER agents_updated_at
    BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE agents IS 'Call center agents who handle Rinkel calls';
COMMENT ON COLUMN agents.external_agent_id IS 'Unique agent identifier from Rinkel system';
COMMENT ON COLUMN agents.metadata IS 'Extensible JSONB for team, department, shift, etc.';


-- ════════════════════════════════════════════════════════════════════════════
-- 2. CALLS
-- ════════════════════════════════════════════════════════════════════════════
-- Central fact table. One row per call received via Rinkel webhook.
-- Tracks the full lifecycle from webhook receipt → audit completion.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE calls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- External references
    external_call_id    TEXT NOT NULL UNIQUE,        -- Rinkel's unique call ID
    agent_id            UUID REFERENCES agents(id)
                            ON DELETE SET NULL,      -- agent may be removed

    -- Source & direction
    source              TEXT NOT NULL DEFAULT 'rinkel',  -- webhook source system
    direction           call_direction NOT NULL DEFAULT 'inbound',

    -- Caller / callee information
    caller_number       TEXT,
    caller_name         TEXT,
    callee_number       TEXT,
    callee_name         TEXT,

    -- Timing
    started_at          TIMESTAMPTZ,                -- when the call started
    ended_at            TIMESTAMPTZ,                -- when the call ended
    duration_seconds    INTEGER DEFAULT 0
                            CHECK (duration_seconds >= 0),
    ring_duration_seconds INTEGER DEFAULT 0
                            CHECK (ring_duration_seconds >= 0),

    -- Processing status
    status              call_status NOT NULL DEFAULT 'received',
    status_changed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Audio
    recording_url       TEXT,                       -- Rinkel's recording URL
    audio_drive_file_id TEXT,                       -- Google Drive file ID after upload
    audio_drive_url     TEXT,                       -- Google Drive shareable link
    audio_duration_seconds  INTEGER,
    audio_format        TEXT DEFAULT 'wav',
    audio_size_bytes    BIGINT,

    -- Transcript reference (denormalised for fast access)
    transcript_status   transcript_status DEFAULT 'pending',

    -- Export tracking
    last_export_id      UUID,                       -- FK added after export_jobs table
    exported_at         TIMESTAMPTZ,

    -- Error handling
    error_message       TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0
                            CHECK (retry_count >= 0),
    last_retry_at       TIMESTAMPTZ,

    -- Extensible metadata
    webhook_payload     JSONB DEFAULT '{}'::JSONB,  -- raw webhook payload for debugging
    metadata            JSONB DEFAULT '{}'::JSONB,  -- application-level metadata
    tags                TEXT[] DEFAULT '{}',         -- searchable tags

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER calls_updated_at
    BEFORE UPDATE ON calls
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE calls IS 'Central call records — one row per Rinkel webhook call event';
COMMENT ON COLUMN calls.external_call_id IS 'Unique call identifier from Rinkel, used for deduplication';
COMMENT ON COLUMN calls.webhook_payload IS 'Raw JSON webhook payload preserved for debugging and replay';
COMMENT ON COLUMN calls.tags IS 'Array of searchable tags for filtering and categorisation';


-- ════════════════════════════════════════════════════════════════════════════
-- 3. TRANSCRIPTS
-- ════════════════════════════════════════════════════════════════════════════
-- Stores Whisper transcription output. Separate table because:
--   - Transcripts can be large (TEXT)
--   - Multiple transcription attempts may exist (retries)
--   - Different models/configs may produce different transcripts
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE transcripts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    call_id             UUID NOT NULL REFERENCES calls(id)
                            ON DELETE CASCADE,

    -- Transcription content
    content             TEXT NOT NULL,               -- full transcript text
    content_length      INTEGER GENERATED ALWAYS AS (LENGTH(content)) STORED,
    language            TEXT DEFAULT 'nl',            -- ISO 639-1 detected language
    confidence_score    NUMERIC(5, 4),               -- 0.0000 – 1.0000

    -- Whisper model info
    model_name          TEXT NOT NULL DEFAULT 'base', -- whisper model used
    model_version       TEXT,

    -- Processing
    status              transcript_status NOT NULL DEFAULT 'pending',
    processing_time_ms  INTEGER,                     -- time to transcribe in ms
    error_message       TEXT,

    -- Segments (for detailed timestamp data)
    segments            JSONB,                       -- [{start, end, text, confidence}, ...]

    -- Metadata
    metadata            JSONB DEFAULT '{}'::JSONB,

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER transcripts_updated_at
    BEFORE UPDATE ON transcripts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

COMMENT ON TABLE transcripts IS 'Whisper transcription output for call audio';
COMMENT ON COLUMN transcripts.segments IS 'Whisper segment-level data: timestamps, text, per-segment confidence';
COMMENT ON COLUMN transcripts.content_length IS 'Auto-computed transcript length for analytics queries';


-- ════════════════════════════════════════════════════════════════════════════
-- 4. WEBHOOK_EVENTS
-- ════════════════════════════════════════════════════════════════════════════
-- Immutable log of every webhook received from Rinkel.
-- Enables replay, debugging, and deduplication.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE webhook_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Source
    source              TEXT NOT NULL DEFAULT 'rinkel',
    event_type          TEXT NOT NULL,                -- e.g. 'call.completed', 'call.started'
    idempotency_key     TEXT UNIQUE,                  -- prevents duplicate processing

    -- Payload
    headers             JSONB DEFAULT '{}'::JSONB,    -- request headers (sanitised)
    payload             JSONB NOT NULL,               -- raw webhook body
    signature           TEXT,                         -- webhook signature for verification

    -- Processing
    status              webhook_status NOT NULL DEFAULT 'received',
    call_id             UUID REFERENCES calls(id)
                            ON DELETE SET NULL,       -- linked after processing
    error_message       TEXT,
    processing_time_ms  INTEGER,

    -- Network info
    ip_address          INET,
    user_agent          TEXT,

    -- Timestamps
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- No updated_at trigger: webhook events are append-only (immutable after processing)

COMMENT ON TABLE webhook_events IS 'Immutable log of all incoming Rinkel webhooks for replay and debugging';
COMMENT ON COLUMN webhook_events.idempotency_key IS 'Deduplication key — rejects duplicate webhook deliveries';


-- ════════════════════════════════════════════════════════════════════════════
-- 5. EXPORT_JOBS
-- ════════════════════════════════════════════════════════════════════════════
-- Tracks JSONL export batches for Claude auditing.
-- Each job exports a range of calls into a JSONL file.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE export_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Job identity
    job_name            TEXT,                         -- human-readable name
    batch_number        INTEGER,

    -- Scope
    filter_criteria     JSONB DEFAULT '{}'::JSONB,    -- filters used to select calls
    call_count          INTEGER NOT NULL DEFAULT 0
                            CHECK (call_count >= 0),
    date_range_start    TIMESTAMPTZ,                  -- earliest call in batch
    date_range_end      TIMESTAMPTZ,                  -- latest call in batch

    -- Output
    file_path           TEXT,                         -- path to exported JSONL file
    file_size_bytes     BIGINT,
    file_checksum       TEXT,                         -- SHA-256 of the export file

    -- Processing
    status              export_status NOT NULL DEFAULT 'pending',
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    processing_time_ms  INTEGER,
    error_message       TEXT,

    -- Metadata
    metadata            JSONB DEFAULT '{}'::JSONB,

    -- Timestamps
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER export_jobs_updated_at
    BEFORE UPDATE ON export_jobs
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Now add the FK from calls.last_export_id → export_jobs.id
ALTER TABLE calls
    ADD CONSTRAINT fk_calls_last_export
    FOREIGN KEY (last_export_id) REFERENCES export_jobs(id)
    ON DELETE SET NULL;

COMMENT ON TABLE export_jobs IS 'JSONL export batches for Claude auditing pipeline';
COMMENT ON COLUMN export_jobs.file_checksum IS 'SHA-256 hash for export file integrity verification';


-- ════════════════════════════════════════════════════════════════════════════
-- 6. AUDIT_LOGS
-- ════════════════════════════════════════════════════════════════════════════
-- System-wide audit trail. Records every significant action for
-- compliance, debugging, and operational visibility.
-- Append-only — rows are never updated or deleted.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- What happened
    action              audit_action NOT NULL,
    description         TEXT,

    -- Who / what was affected
    agent_id            UUID REFERENCES agents(id) ON DELETE SET NULL,
    call_id             UUID REFERENCES calls(id) ON DELETE SET NULL,
    entity_type         TEXT,                         -- 'call', 'agent', 'export', etc.
    entity_id           UUID,                         -- generic FK to any entity

    -- Context
    old_values          JSONB,                        -- previous state (for updates)
    new_values          JSONB,                        -- new state (for updates)
    metadata            JSONB DEFAULT '{}'::JSONB,

    -- Source
    source              TEXT DEFAULT 'system',        -- 'system', 'webhook', 'api', 'cron'
    ip_address          INET,
    user_agent          TEXT,

    -- Timestamp (append-only, no updated_at)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE audit_logs IS 'Append-only audit trail for compliance and operational debugging';
COMMENT ON COLUMN audit_logs.old_values IS 'JSONB snapshot of entity state before modification';
COMMENT ON COLUMN audit_logs.new_values IS 'JSONB snapshot of entity state after modification';

COMMIT;
