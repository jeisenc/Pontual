-- Pontual: rider ratings ("Does this match your experience of this train?")
--
-- Run once in Supabase: Dashboard → SQL Editor → New query → paste → Run.
-- Visitors can only use the two functions below. They can't read or edit the
-- table directly, so no one sees who voted what.

create table if not exists public.ratings (
  train      integer     not null check (train > 0),
  voter      uuid        not null,          -- random id kept in the visitor's browser
  verdict    text        not null check (verdict in ('match', 'worse', 'better')),
  updated_at timestamptz not null default now(),
  primary key (train, voter)
);

-- Lock the table: row-level security on, no policies → no direct access.
alter table public.ratings enable row level security;
revoke all on public.ratings from anon, authenticated;

-- Cast, change or clear (p_verdict = null) one visitor's rating of one train.
create or replace function public.cast_rating(p_train integer, p_voter uuid, p_verdict text)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_train is null or p_train <= 0 or p_voter is null then
    raise exception 'bad request';
  end if;

  if p_verdict is null then
    delete from ratings where train = p_train and voter = p_voter;
    return;
  end if;

  if p_verdict not in ('match', 'worse', 'better') then
    raise exception 'bad verdict';
  end if;

  -- Light abuse guard: one browser can rate at most 300 different trains.
  if not exists (select 1 from ratings where train = p_train and voter = p_voter)
     and (select count(*) from ratings where voter = p_voter) >= 300 then
    raise exception 'too many ratings';
  end if;

  insert into ratings (train, voter, verdict, updated_at)
  values (p_train, p_voter, p_verdict, now())
  on conflict (train, voter)
  do update set verdict = excluded.verdict, updated_at = now();
end;
$$;

-- Totals for every train, read once when the dashboard opens.
create or replace function public.rating_totals()
returns table (train integer, matches integer, worse integer, better integer)
language sql
stable
security definer
set search_path = public
as $$
  select train,
         count(*) filter (where verdict = 'match')::int,
         count(*) filter (where verdict = 'worse')::int,
         count(*) filter (where verdict = 'better')::int
  from ratings
  group by train;
$$;

revoke execute on function public.cast_rating(integer, uuid, text) from public;
revoke execute on function public.rating_totals() from public;
grant execute on function public.cast_rating(integer, uuid, text) to anon, authenticated;
grant execute on function public.rating_totals() to anon, authenticated;
