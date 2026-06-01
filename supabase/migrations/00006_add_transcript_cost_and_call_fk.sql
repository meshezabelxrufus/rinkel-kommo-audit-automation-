-- ============================================================================
-- Migration 00006: Add cost_usd to transcripts, transcript_id to calls,
--                  and whisper_segments computed column
-- ============================================================================
--
-- WHY THESE COLUMNS:
--
-- transcripts.cost_usd
--   The Whisper integration (app/integrations/whisper.py) already tracks
--   cost_usd in the TranscriptionResult dataclass and passes it to the
--   transcript repository. Without this column the value is silently
--   discarded. Adding it enables:
--     - Per-call cost tracking for budget monitoring
--     - Aggregate cost analytics per agent / time period
--     - Cost reporting in export_jobs metadata
--
-- calls.transcript_id
--   The export repository joins calls → transcripts to build audit records.
--   A direct FK on calls makes that join trivial (no subquery needed) and
--   ensures referential integrity when a transcript is produced.
--   The calls table already has transcript_status; transcript_id is the
--   natural companion that removes ambiguity when multiple transcript
--   attempts exist (only the authoritative one is linked here).
--
-- transcripts.word_count (generated column)
--   Derived from existing `content` column. Zero cost to maintain (STORED).
--   Enables fast analytics without a full-text scan.
--   Named word_count rather than whisper_segments to align with the SQL
--   convention of storing raw segments as JSONB in the segments column.
--
-- ============================================================================

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- 1. transcripts: add cost_usd
-- ────────────────────────────────────────────────────────────────────────────
-- Whisper pricing: $0.006 / minute (as of 2025).
-- Stored as NUMERIC(10, 6) to accommodate both sub-cent amounts and
-- multi-hour recordings without floating-point precision loss.
-- NULL = cost not yet calculated (e.g. pending transcripts).

ALTER TABLE transcripts
    ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10, 6)
        CHECK (cost_usd >= 0);

COMMENT ON COLUMN transcripts.cost_usd IS
    'Estimated Whisper API cost in USD. $0.006 / minute (Whisper v2 pricing).
     NULL = cost not yet computed (transcript pending or pre-migration row).';


-- ────────────────────────────────────────────────────────────────────────────
-- 2. transcripts: add word_count generated column
-- ────────────────────────────────────────────────────────────────────────────
-- Approximate word count derived from content using array_length on
-- regexp_split_to_array. Stored (GENERATED ALWAYS AS ... STORED) so it
-- is maintained automatically on INSERT/UPDATE with zero application code.

ALTER TABLE transcripts
    ADD COLUMN IF NOT EXISTS word_count INTEGER
        GENERATED ALWAYS AS (
            array_length(
                regexp_split_to_array(trim(content), '\s+'),
                1
            )
        ) STORED;

COMMENT ON COLUMN transcripts.word_count IS
    'Approximate word count derived from content. Auto-computed on write.
     Used for quick analytics without full-text scanning.';


-- ────────────────────────────────────────────────────────────────────────────
-- 3. calls: add transcript_id FK → transcripts
-- ────────────────────────────────────────────────────────────────────────────
-- Points to the authoritative transcript for this call.
-- NULL for calls that have not yet been transcribed.
-- ON DELETE SET NULL: safe — the call record survives transcript deletion.
--
-- Note: the transcripts table has call_id → calls, so this creates a
-- "mutual reference". This is intentional:
--   - transcripts.call_id  → find all transcripts for a call
--   - calls.transcript_id  → fast access to the authoritative transcript

ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS transcript_id UUID
        REFERENCES transcripts(id)
        ON DELETE SET NULL;

COMMENT ON COLUMN calls.transcript_id IS
    'FK to the authoritative transcript for this call.
     NULL = not yet transcribed or transcription pending.
     Multiple transcript attempts may exist in the transcripts table
     (for retries/re-runs); this column points to the one in production use.';


-- ────────────────────────────────────────────────────────────────────────────
-- 4. Indexes for the new columns
-- ────────────────────────────────────────────────────────────────────────────

-- cost_usd: range queries for billing reports (e.g. "calls that cost > $0.05")
CREATE INDEX IF NOT EXISTS idx_transcripts_cost_usd
    ON transcripts (cost_usd)
    WHERE cost_usd IS NOT NULL;

-- transcript_id: join from calls → transcripts without a scan
CREATE INDEX IF NOT EXISTS idx_calls_transcript_id
    ON calls (transcript_id)
    WHERE transcript_id IS NOT NULL;

-- word_count: analytics queries (e.g. "transcripts longer than 500 words")
CREATE INDEX IF NOT EXISTS idx_transcripts_word_count
    ON transcripts (word_count)
    WHERE word_count IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- 5. Update the export view to include new columns
-- ────────────────────────────────────────────────────────────────────────────
-- The export view is defined in 00005. We replace it to include cost_usd
-- and word_count so JSONL exports capture cost data for analytics.

CREATE OR REPLACE VIEW calls_export_ready AS
SELECT
    c.id                    AS call_id,
    c.external_call_id,
    c.direction,
    c.source,
    c.caller_number,
    c.caller_name,
    c.callee_number,
    c.callee_name,
    c.started_at,
    c.ended_at,
    c.duration_seconds,
    c.status                AS call_status,
    c.recording_url,
    c.audio_drive_url,
    c.transcript_status,
    c.transcript_id,        -- ← new: direct FK reference
    c.exported_at,
    c.last_export_id,

    -- Agent info
    a.id                    AS agent_id,
    a.external_agent_id     AS agent_external_id,
    a.display_name          AS agent_name,
    a.email                 AS agent_email,

    -- Transcript info
    t.id                    AS transcript_id_full,
    t.content               AS transcript_content,
    t.language              AS transcript_language,
    t.confidence_score      AS transcript_confidence,
    t.model_name            AS transcript_model,
    t.segments              AS transcript_segments,
    t.cost_usd              AS transcript_cost_usd,    -- ← new
    t.word_count            AS transcript_word_count,  -- ← new
    t.processing_time_ms    AS transcript_processing_ms

FROM calls c
LEFT JOIN agents     a ON a.id = c.agent_id
LEFT JOIN transcripts t ON t.id = c.transcript_id      -- ← uses new FK
WHERE
    c.status IN ('completed', 'transcribed', 'exported')
    AND c.exported_at IS NULL;

COMMENT ON VIEW calls_export_ready IS
    'Calls ready for JSONL export — joined with agent and transcript data.
     Updated in migration 00006 to include transcript_id FK,
     cost_usd, and word_count.';


-- ────────────────────────────────────────────────────────────────────────────
-- 6. Backfill transcript_id for existing completed calls
-- ────────────────────────────────────────────────────────────────────────────
-- For calls that already have transcripts, link the most recent one.
-- Uses DISTINCT ON with ORDER BY created_at DESC to pick the newest.

UPDATE calls c
SET transcript_id = latest.id
FROM (
    SELECT DISTINCT ON (call_id)
        id,
        call_id
    FROM transcripts
    WHERE status = 'completed'
    ORDER BY call_id, created_at DESC
) latest
WHERE latest.call_id = c.id
  AND c.transcript_id IS NULL;


COMMIT;
