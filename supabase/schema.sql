-- Reference schema. Applied to Supabase as migrations:
-- create_transcription_logs / deny_public_transcription_log_access
create table public.transcription_logs (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  status text not null check (status in ('success', 'error')),
  original_filename text,
  audio_duration_seconds numeric(12,3) check (audio_duration_seconds is null or audio_duration_seconds >= 0),
  raw_characters integer check (raw_characters is null or raw_characters >= 0),
  output_characters integer check (output_characters is null or output_characters >= 0),
  transcription_model text not null,
  transcription_segments integer check (transcription_segments is null or transcription_segments >= 0),
  transcription_cost_usd numeric(14,8) not null default 0 check (transcription_cost_usd >= 0),
  polish_requested boolean not null default false,
  polish_succeeded boolean not null default false,
  polish_partial boolean not null default false,
  polish_model text,
  polish_input_tokens integer not null default 0 check (polish_input_tokens >= 0),
  polish_output_tokens integer not null default 0 check (polish_output_tokens >= 0),
  polish_cost_usd numeric(14,8) not null default 0 check (polish_cost_usd >= 0),
  total_cost_usd numeric(14,8) generated always as (transcription_cost_usd + polish_cost_usd) stored,
  processing_seconds numeric(12,3) check (processing_seconds is null or processing_seconds >= 0),
  error_type text,
  error_message text
);

comment on table public.transcription_logs is
  'Usage metadata only. Audio files and transcription text are never stored.';

create index transcription_logs_created_at_idx on public.transcription_logs (created_at desc);
create index transcription_logs_status_created_at_idx on public.transcription_logs (status, created_at desc);
create index transcription_logs_model_created_at_idx on public.transcription_logs (transcription_model, created_at desc);

alter table public.transcription_logs enable row level security;
revoke all on table public.transcription_logs from anon, authenticated;
grant select, insert on table public.transcription_logs to service_role;

create policy "deny public access"
on public.transcription_logs as restrictive for all to anon, authenticated
using (false) with check (false);
