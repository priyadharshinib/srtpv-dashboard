-- Run in Supabase > SQL Editor AFTER the Streamlit app has connected once
-- (that first run creates the "solar" schema and its tables).
-- Replace the password before running.

create role grafana_reader with login password 'CHANGE-ME-strong-password';

grant usage on schema solar to grafana_reader;
grant select on all tables in schema solar to grafana_reader;
alter default privileges in schema solar grant select on tables to grafana_reader;

-- Check: should list 5 tables
select table_name from information_schema.tables where table_schema = 'solar';
