-- ============================================================================
-- Migration 00003: Create indexes
-- ============================================================================
-- Index strategy:
--   - B-tree for equality/range lookups (default)
--   - GIN for JSONB containment queries and array search
--   - Partial indexes for hot-path queries (e.g. active statuses only)
--   - Composite indexes for common multi-column filters
--
-- Naming convention: idx_{table}_{columns}[_{qualifier}]
-- ============================================================================

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- AGENTS indexes
-- ────────────────────────────────────────────────────────────────────────────

-- Fast lookup by external ID (already UNIQUE, so implicitly indexed)
-- Additional index on active agents for dropdown/selection queries
CREATE INDEX idx_agents_active
    ON agents (display_name)
    WHERE is_active = TRUE;

-- Email lookup (for integration matching)
CREATE INDEX idx_agents_email
    ON agents (email)
    WHERE email IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- CALLS indexes
-- ────────────────────────────────────────────────────────────────────────────

-- Status-based queries are the #1 hot path (pipeline workers poll by status)
CREATE INDEX idx_calls_status
    ON calls (status);

-- Partial index: only calls that need processing (excludes terminal states)
-- This is the primary index the pipeline worker will scan
CREATE INDEX idx_calls_status_pending
    ON calls (status, created_at)
    WHERE status IN ('received', 'downloading', 'uploading', 'transcribing');

-- Agent performance queries: "show all calls for agent X"
CREATE INDEX idx_calls_agent_id
    ON calls (agent_id)
    WHERE agent_id IS NOT NULL;

-- Composite: agent + status (agent performance dashboards)
CREATE INDEX idx_calls_agent_status
    ON calls (agent_id, status)
    WHERE agent_id IS NOT NULL;

-- Time-range queries: "all calls between date X and Y"
CREATE INDEX idx_calls_started_at
    ON calls (started_at DESC)
    WHERE started_at IS NOT NULL;

-- Created_at for pagination and chronological listing
CREATE INDEX idx_calls_created_at
    ON calls (created_at DESC);

-- Direction filter: "all inbound calls" / "all outbound calls"
CREATE INDEX idx_calls_direction
    ON calls (direction);

-- Phone number lookups (caller investigation)
CREATE INDEX idx_calls_caller_number
    ON calls (caller_number)
    WHERE caller_number IS NOT NULL;

CREATE INDEX idx_calls_callee_number
    ON calls (callee_number)
    WHERE callee_number IS NOT NULL;

-- Transcript status for bulk operations ("all calls needing transcription")
CREATE INDEX idx_calls_transcript_status
    ON calls (transcript_status)
    WHERE transcript_status IN ('pending', 'processing', 'failed');

-- Export tracking: unexported calls
CREATE INDEX idx_calls_unexported
    ON calls (created_at)
    WHERE exported_at IS NULL AND status = 'transcribed';

-- Source filter (future-proofing for multi-source ingestion)
CREATE INDEX idx_calls_source
    ON calls (source);

-- Tags: GIN index for array containment (@> operator)
CREATE INDEX idx_calls_tags
    ON calls USING GIN (tags);

-- Metadata: GIN index for JSONB queries
CREATE INDEX idx_calls_metadata
    ON calls USING GIN (metadata jsonb_path_ops);

-- Error tracking: failed calls for retry dashboard
CREATE INDEX idx_calls_failed
    ON calls (status, retry_count, last_retry_at)
    WHERE status IN ('download_failed', 'upload_failed', 'transcription_failed');


-- ────────────────────────────────────────────────────────────────────────────
-- TRANSCRIPTS indexes
-- ────────────────────────────────────────────────────────────────────────────

-- One-to-many: find all transcripts for a call
CREATE INDEX idx_transcripts_call_id
    ON transcripts (call_id);

-- Status-based processing queries
CREATE INDEX idx_transcripts_status
    ON transcripts (status)
    WHERE status IN ('pending', 'processing', 'failed');

-- Model analysis: "how did model X perform?"
CREATE INDEX idx_transcripts_model
    ON transcripts (model_name);

-- Language distribution analytics
CREATE INDEX idx_transcripts_language
    ON transcripts (language);

-- Full-text search on transcript content
CREATE INDEX idx_transcripts_content_fts
    ON transcripts USING GIN (to_tsvector('dutch', content));


-- ────────────────────────────────────────────────────────────────────────────
-- WEBHOOK_EVENTS indexes
-- ────────────────────────────────────────────────────────────────────────────

-- Idempotency check (already UNIQUE, implicitly indexed)

-- Status-based processing
CREATE INDEX idx_webhook_events_status
    ON webhook_events (status);

-- Chronological listing and time-range queries
CREATE INDEX idx_webhook_events_received_at
    ON webhook_events (received_at DESC);

-- Link to processed call
CREATE INDEX idx_webhook_events_call_id
    ON webhook_events (call_id)
    WHERE call_id IS NOT NULL;

-- Event type analysis
CREATE INDEX idx_webhook_events_event_type
    ON webhook_events (event_type);

-- Source + type composite for multi-source filtering
CREATE INDEX idx_webhook_events_source_type
    ON webhook_events (source, event_type);

-- Failed webhooks for retry/investigation
CREATE INDEX idx_webhook_events_failed
    ON webhook_events (status, received_at)
    WHERE status = 'failed';

-- GIN index on payload for JSONB queries
CREATE INDEX idx_webhook_events_payload
    ON webhook_events USING GIN (payload jsonb_path_ops);


-- ────────────────────────────────────────────────────────────────────────────
-- EXPORT_JOBS indexes
-- ────────────────────────────────────────────────────────────────────────────

-- Status-based queries
CREATE INDEX idx_export_jobs_status
    ON export_jobs (status);

-- Chronological listing
CREATE INDEX idx_export_jobs_created_at
    ON export_jobs (created_at DESC);

-- Active jobs (for preventing overlapping exports)
CREATE INDEX idx_export_jobs_active
    ON export_jobs (status)
    WHERE status IN ('pending', 'running');


-- ────────────────────────────────────────────────────────────────────────────
-- AUDIT_LOGS indexes
-- ────────────────────────────────────────────────────────────────────────────

-- Action type queries (most common audit log query)
CREATE INDEX idx_audit_logs_action
    ON audit_logs (action);

-- Chronological listing (time-range compliance queries)
CREATE INDEX idx_audit_logs_created_at
    ON audit_logs (created_at DESC);

-- Entity-based audit trail: "what happened to call X?"
CREATE INDEX idx_audit_logs_call_id
    ON audit_logs (call_id)
    WHERE call_id IS NOT NULL;

-- Agent activity trail
CREATE INDEX idx_audit_logs_agent_id
    ON audit_logs (agent_id)
    WHERE agent_id IS NOT NULL;

-- Generic entity lookup
CREATE INDEX idx_audit_logs_entity
    ON audit_logs (entity_type, entity_id)
    WHERE entity_id IS NOT NULL;

-- Composite: action + time for filtered timeline queries
CREATE INDEX idx_audit_logs_action_time
    ON audit_logs (action, created_at DESC);

COMMIT;
