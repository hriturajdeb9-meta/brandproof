-- Brandproof: database-enforced roles (PostgreSQL row-level security).
-- Run automatically at start-up when DATABASE_URL points to PostgreSQL (for example Supabase). Idempotent.
--
-- The app connects as the database owner for sign-in and set-up work only. For everything a user does it
-- switches to the restricted role bp_app and sets app.user_id and app.workspace_id. These policies then
-- look up that user's membership row and decide, inside the database, what they may read or change:
--   * nobody sees another workspace's data
--   * only the brand manager can change the Brand Kit, members or guideline imports
--   * creators (and brand managers) write drafts, versions and fix decisions; reviewers cannot
--   * the audit log is append-only: nobody can edit or delete it
-- Supabase's public API roles (anon, authenticated) get no access at all; Brandproof does not use them.

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bp_app') THEN
    CREATE ROLE bp_app NOLOGIN;
  END IF;
END $$;
GRANT bp_app TO CURRENT_USER;

CREATE OR REPLACE FUNCTION bp_uid() RETURNS integer LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.user_id', true), '')::integer $$;
CREATE OR REPLACE FUNCTION bp_ws() RETURNS integer LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.workspace_id', true), '')::integer $$;
-- SECURITY DEFINER: reads the membership table without being subject to its own policies
CREATE OR REPLACE FUNCTION bp_role(ws integer) RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = public, pg_temp AS
  $$ SELECT role FROM members WHERE workspace_id = ws AND user_id = bp_uid() LIMIT 1 $$;
REVOKE ALL ON FUNCTION bp_uid(), bp_ws(), bp_role(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION bp_uid(), bp_ws(), bp_role(integer) TO bp_app;

GRANT USAGE ON SCHEMA public TO bp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bp_app;
REVOKE ALL ON meta, users FROM bp_app;
GRANT SELECT, UPDATE ON workspaces TO bp_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON members, rules, rule_versions, glossary, facts, palette, confidential,
  drafts, versions, decisions, guidelines, candidates TO bp_app;
GRANT SELECT, INSERT ON comments, audit, ai_calls, ai_cache TO bp_app;
REVOKE UPDATE, DELETE ON comments, audit FROM bp_app;

-- keep Supabase's auto-generated REST API away from these tables
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
      EXECUTE format('REVOKE ALL ON FUNCTION bp_uid(), bp_ws(), bp_role(integer) FROM %I', r);
    END IF;
  END LOOP;
END $$;

-- turn on row-level security everywhere (tables without a bp_app policy are closed to bp_app)
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['meta', 'users', 'workspaces', 'members', 'rules', 'rule_versions', 'glossary', 'facts',
    'palette', 'confidential', 'drafts', 'versions', 'decisions', 'comments', 'audit', 'guidelines', 'candidates',
    'ai_calls', 'ai_cache'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;

-- (re)create policies
DO $$
DECLARE
  t text;
  pol record;
  member_read text := 'workspace_id = bp_ws() AND bp_role(workspace_id) IS NOT NULL';
  brand_only text := 'workspace_id = bp_ws() AND bp_role(workspace_id) = ''brand''';
  writers text := 'workspace_id = bp_ws() AND bp_role(workspace_id) IN (''creator'', ''brand'')';
BEGIN
  FOR pol IN SELECT policyname, tablename FROM pg_policies WHERE schemaname = 'public' AND policyname LIKE 'bp\_%' LOOP
    EXECUTE format('DROP POLICY %I ON %I', pol.policyname, pol.tablename);
  END LOOP;

  EXECUTE 'CREATE POLICY bp_read ON workspaces FOR SELECT TO bp_app USING (bp_role(id) IS NOT NULL)';
  EXECUTE 'CREATE POLICY bp_update ON workspaces FOR UPDATE TO bp_app USING (id = bp_ws() AND bp_role(id) = ''brand'')';

  -- Brand Kit and team: everyone in the workspace reads, only the brand manager writes
  FOREACH t IN ARRAY ARRAY['members', 'rules', 'rule_versions', 'glossary', 'facts', 'palette', 'confidential'] LOOP
    EXECUTE format('CREATE POLICY bp_read ON %I FOR SELECT TO bp_app USING (%s)', t, member_read);
    EXECUTE format('CREATE POLICY bp_insert ON %I FOR INSERT TO bp_app WITH CHECK (%s)', t, brand_only);
    EXECUTE format('CREATE POLICY bp_update ON %I FOR UPDATE TO bp_app USING (%s) WITH CHECK (%s)', t, brand_only, brand_only);
    EXECUTE format('CREATE POLICY bp_delete ON %I FOR DELETE TO bp_app USING (%s)', t, brand_only);
  END LOOP;

  -- Guideline imports and their draft rules: brand manager only
  FOREACH t IN ARRAY ARRAY['guidelines', 'candidates'] LOOP
    EXECUTE format('CREATE POLICY bp_all ON %I FOR ALL TO bp_app USING (%s) WITH CHECK (%s)', t, brand_only, brand_only);
  END LOOP;

  -- Drafts: creators and brand managers create; everyone in the workspace can update (reviewers set the status)
  EXECUTE format('CREATE POLICY bp_read ON drafts FOR SELECT TO bp_app USING (%s)', member_read);
  EXECUTE format('CREATE POLICY bp_insert ON drafts FOR INSERT TO bp_app WITH CHECK (%s)', writers);
  EXECUTE format('CREATE POLICY bp_update ON drafts FOR UPDATE TO bp_app USING (%s) WITH CHECK (%s)', member_read, member_read);
  EXECUTE format('CREATE POLICY bp_delete ON drafts FOR DELETE TO bp_app USING (%s)', brand_only);

  -- Versions and fix decisions: only creators and brand managers write
  FOREACH t IN ARRAY ARRAY['versions', 'decisions'] LOOP
    EXECUTE format('CREATE POLICY bp_read ON %I FOR SELECT TO bp_app USING (%s)', t, member_read);
    EXECUTE format('CREATE POLICY bp_insert ON %I FOR INSERT TO bp_app WITH CHECK (%s)', t, writers);
    EXECUTE format('CREATE POLICY bp_update ON %I FOR UPDATE TO bp_app USING (%s) WITH CHECK (%s)', t, writers, writers);
    EXECUTE format('CREATE POLICY bp_delete ON %I FOR DELETE TO bp_app USING (%s)', t, writers);
  END LOOP;

  -- Comments and the audit log: everyone in the workspace adds; nobody edits or deletes (no grant either)
  FOREACH t IN ARRAY ARRAY['comments', 'audit'] LOOP
    EXECUTE format('CREATE POLICY bp_read ON %I FOR SELECT TO bp_app USING (%s)', t, member_read);
    EXECUTE format('CREATE POLICY bp_insert ON %I FOR INSERT TO bp_app WITH CHECK (%s)', t, member_read);
  END LOOP;

  -- shared AI answer cache and call log (contain no workspace data beyond hashes and timings)
  EXECUTE 'CREATE POLICY bp_read ON ai_cache FOR SELECT TO bp_app USING (true)';
  EXECUTE 'CREATE POLICY bp_insert ON ai_cache FOR INSERT TO bp_app WITH CHECK (true)';
  EXECUTE 'CREATE POLICY bp_read ON ai_calls FOR SELECT TO bp_app USING (true)';
  EXECUTE 'CREATE POLICY bp_insert ON ai_calls FOR INSERT TO bp_app WITH CHECK (true)';
END $$;
