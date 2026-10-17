-- The docs table as created by koya-app/db/schema.sql (copied 17 Oct 2026), used ONLY by the nightly restore drill
-- to build a throwaway local copy. Production triggers, realtime and RLS policies are not needed to prove the data restores.
create table if not exists public.docs (
  path text primary key,            -- "collection/doc_id"
  coll text not null,
  id text not null,
  data jsonb not null default '{}'::jsonb,
  version int not null default 1,
  updated_at timestamptz not null default now(),
  updated_by text
);
create index if not exists docs_coll_idx on public.docs (coll, updated_at desc);
alter table public.docs enable row level security;

-- the roles a Supabase REST call runs as (PostgREST switches to the role in the key)
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
end $$;
grant usage on schema public to anon, service_role;
grant all on public.docs to service_role;
