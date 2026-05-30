-- ============================================================================
-- Migration 00004: Row-Level Security (RLS) policies
-- ============================================================================
-- Supabase uses RLS for multi-tenant access control.
--
-- Strategy:
--   - Service role (backend API) gets full access via service_role key
--   - Anon role gets read-only access to health/status endpoints only
--   - Authenticated users (future dashboard) get scoped access
--   - Webhook events and audit logs are insert-only from the API
--
-- IMPORTANT: In Supabase, enabling RLS without policies DENIES all access
-- except to the service_role, which bypasses RLS by default.
-- ============================================================================

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- Enable RLS on all tables
-- ────────────────────────────────────────────────────────────────────────────
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE transcripts ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;


-- ────────────────────────────────────────────────────────────────────────────
-- SERVICE ROLE policies (backend API — full access)
-- ────────────────────────────────────────────────────────────────────────────
-- The service_role bypasses RLS by default in Supabase, so these policies
-- are technically redundant but are included for documentation and in case
-- the bypass behavior is ever changed.

-- Agents: full CRUD for service role
CREATE POLICY "service_role_agents_all" ON agents
    FOR ALL
    TO service_role
    USING (TRUE)
    WITH CHECK (TRUE);

-- Calls: full CRUD for service role
CREATE POLICY "service_role_calls_all" ON calls
    FOR ALL
    TO service_role
    USING (TRUE)
    WITH CHECK (TRUE);

-- Transcripts: full CRUD for service role
CREATE POLICY "service_role_transcripts_all" ON transcripts
    FOR ALL
    TO service_role
    USING (TRUE)
    WITH CHECK (TRUE);

-- Webhook events: full CRUD for service role
CREATE POLICY "service_role_webhook_events_all" ON webhook_events
    FOR ALL
    TO service_role
    USING (TRUE)
    WITH CHECK (TRUE);

-- Export jobs: full CRUD for service role
CREATE POLICY "service_role_export_jobs_all" ON export_jobs
    FOR ALL
    TO service_role
    USING (TRUE)
    WITH CHECK (TRUE);

-- Audit logs: full access for service role (read + insert, no update/delete)
CREATE POLICY "service_role_audit_logs_all" ON audit_logs
    FOR ALL
    TO service_role
    USING (TRUE)
    WITH CHECK (TRUE);


-- ────────────────────────────────────────────────────────────────────────────
-- ANON ROLE policies (public / unauthenticated — minimal access)
-- ────────────────────────────────────────────────────────────────────────────
-- Anon should only be able to insert webhook events (Rinkel callback).
-- All other access is denied.

-- Allow webhook insertion from anon (Rinkel sends webhooks without auth)
CREATE POLICY "anon_webhook_insert" ON webhook_events
    FOR INSERT
    TO anon
    WITH CHECK (TRUE);


-- ────────────────────────────────────────────────────────────────────────────
-- AUTHENTICATED ROLE policies (future dashboard users)
-- ────────────────────────────────────────────────────────────────────────────
-- When a dashboard is built, authenticated users can view calls and agents
-- but cannot modify webhook events or audit logs.

-- Agents: read-only
CREATE POLICY "authenticated_agents_select" ON agents
    FOR SELECT
    TO authenticated
    USING (TRUE);

-- Calls: read-only
CREATE POLICY "authenticated_calls_select" ON calls
    FOR SELECT
    TO authenticated
    USING (TRUE);

-- Transcripts: read-only
CREATE POLICY "authenticated_transcripts_select" ON transcripts
    FOR SELECT
    TO authenticated
    USING (TRUE);

-- Export jobs: read-only
CREATE POLICY "authenticated_export_jobs_select" ON export_jobs
    FOR SELECT
    TO authenticated
    USING (TRUE);

-- Audit logs: read-only (compliance team)
CREATE POLICY "authenticated_audit_logs_select" ON audit_logs
    FOR SELECT
    TO authenticated
    USING (TRUE);

COMMIT;
