-- Grafana alert: each inverter's output on the latest day, as a ratio of the median inverter.
-- Condition in Grafana: ratio IS BELOW 0.5
with latest as (
    select max(date) as day from solar.inverter_readings
),
r as (
    select i.inverter, i.kwh
    from solar.inverter_readings i, latest
    where i.date = latest.day
)
select
    inverter,
    round((kwh / nullif((select percentile_cont(0.5) within group (order by kwh) from r), 0))::numeric, 2) as ratio
from r;
