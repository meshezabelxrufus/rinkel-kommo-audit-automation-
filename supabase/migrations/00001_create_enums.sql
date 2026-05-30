-- ============================================================================
-- Migration 00001: Create custom enum types
-- ============================================================================
-- These enums enforce valid state machines at the database level.
-- Using PostgreSQL enums instead of CHECK constraints for:
--   1. Better query plan optimization
--   2. Centralised vocabulary (one ALTER TYPE vs many ALTER TABLE)
--   3. Type safety in application code
-- ============================================================================

BEGIN;

-- Call direction: inbound calls received vs outbound calls placed
CREATE TYPE call_direction AS ENUM (
    'inbound',
    'outbound',
    'internal'
);

-- Call processing status — models the full lifecycle
CREATE TYPE call_status AS ENUM (
    'received',          -- webhook received, record created
    'downloading',       -- downloading audio from Rinkel
    'download_failed',   -- audio download failed
    'uploading',         -- uploading audio to Google Drive
    'upload_failed',     -- Drive upload failed
    'transcribing',      -- Whisper transcription in progress
    'transcription_failed',  -- transcription failed
    'transcribed',       -- transcript available
    'exporting',         -- included in JSONL export batch
    'exported',          -- JSONL export complete
    'audited',           -- Claude audit complete
    'archived'           -- final state
);

-- Transcript processing status
CREATE TYPE transcript_status AS ENUM (
    'pending',
    'processing',
    'completed',
    'failed',
    'retrying'
);

-- Webhook event processing status
CREATE TYPE webhook_status AS ENUM (
    'received',
    'validated',
    'processing',
    'processed',
    'failed',
    'ignored'       -- duplicate or irrelevant event
);

-- Export job status
CREATE TYPE export_status AS ENUM (
    'pending',
    'running',
    'completed',
    'failed',
    'cancelled'
);

-- Audit log severity / category
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

COMMIT;
