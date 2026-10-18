-- The Team chat v2 tables as created by koya-app db/chat/2026-10-18_chat_v2.sql (copied 18 Oct 2026), used ONLY by the nightly
-- restore drill to build a throwaway local copy. Rules, functions, triggers and realtime are not needed to prove the data restores.
create table if not exists public.chat_channels (
  id uuid primary key default gen_random_uuid(), key text unique, name text not null default '', topic text not null default '',
  section text not null default 'company', kind text not null default 'channel', see text not null default 'all', depts text[] not null default '{}',
  post text not null default 'all', sort int not null default 100, dm_key text unique, archived_at timestamptz, created_by uuid, created_at timestamptz not null default now());
create table if not exists public.chat_members (
  channel_id uuid not null references public.chat_channels(id) on delete cascade, user_id uuid not null, role text not null default 'reader',
  last_read_at timestamptz, muted boolean not null default false, joined_at timestamptz not null default now(), primary key (channel_id, user_id));
create table if not exists public.chat_messages (
  id text primary key, channel_id uuid not null references public.chat_channels(id) on delete cascade, thread_id text references public.chat_messages(id) on delete cascade,
  author_id uuid, kind text not null default 'user', body jsonb not null default '{}'::jsonb, mentions uuid[] not null default '{}', here boolean not null default false,
  everyone boolean not null default false, pinned_at timestamptz, pinned_by uuid, reply_count int not null default 0, last_reply_at timestamptz,
  created_at timestamptz not null default now(), edited_at timestamptz, updated_at timestamptz not null default now());
create table if not exists public.chat_reactions (
  message_id text not null references public.chat_messages(id) on delete cascade, user_id uuid not null, emoji text not null,
  channel_id uuid not null references public.chat_channels(id) on delete cascade, created_at timestamptz not null default now(), primary key (message_id, user_id, emoji));
create table if not exists public.chat_saves (
  user_id uuid not null, message_id text not null references public.chat_messages(id) on delete cascade, created_at timestamptz not null default now(), primary key (user_id, message_id));
grant all on public.chat_channels, public.chat_members, public.chat_messages, public.chat_reactions, public.chat_saves to service_role;
