"""Run with:  pytest -q"""
import io

import pandas as pd
import pytest

from core.calculations import (
    daily_statement,
    flagged_cells,
    headline_kpis,
    inverter_totals,
    outage_mask,
)
from core.importer import import_ht_meter, import_inverter, import_lt_meter, load_sample


@pytest.fixture(scope="module")
def sample():
    return load_sample()


def csv(text: str, name: str = "x.csv"):
    f = io.StringIO(text)
    f.name = name
    return f


# ---------- calculations (expected values match the October 2026 report) ----------

def test_headline_totals(sample):
    inv, lt, ht = sample
    k = headline_kpis(inv, lt, ht, pd.Timestamp("2026-10-12"))
    assert k["inverter_kwh"] == pytest.approx(40617.29, abs=0.01)
    assert k["lt_kwh"] == 44070
    assert k["ht_import_kwh"] == 118908
    assert k["ht_export_kwh"] == 1872
    assert k["site_load_kwh"] == 117036 + 44070
    assert k["solar_share"] == pytest.approx(0.2735, abs=1e-4)
    assert k["lt_vs_inverter"] == pytest.approx(0.0082, abs=1e-4)


def test_pv4_is_chronically_low(sample):
    t = inverter_totals(sample[0]).set_index("inverter")
    assert t.loc["PV4", "kwh"] == pytest.approx(1388.03, abs=0.01)
    assert t.loc["PV4", "vs_others"] < 0.3
    assert t.loc["PV4", "zero_days"] == ["01 Oct", "10 Oct", "11 Oct"]


def test_outage_only_on_11_oct(sample):
    inv = sample[0]
    assert inv.loc[outage_mask(inv), "date"].dt.day.tolist() == [11]


def test_flagged_cells(sample):
    flags = flagged_cells(sample[0])
    assert flags["PV4"].sum() == 10
    assert flags["PV1"].sum() == 0


def test_daily_statement_lt_only_on_first_day_of_period(sample):
    d = daily_statement(*sample).set_index(sample[0]["date"].dt.day)
    assert d.loc[1, "lt_kwh"] == 7740 and pd.isna(d.loc[2, "lt_kwh"])
    assert d.loc[2, "lt_period"] == "01–02 Oct"


# ---------- import validation ----------

INV_HEADER = "date,PV1,PV2,PV3,PV4,PV5,PV6,PV7,PV8,PV9,Total\n"


def test_inverter_missing_column():
    res = import_inverter(csv("date,PV1\n2026-10-01,5\n"))
    assert not res.ok and "missing columns" in res.errors[0]


def test_inverter_duplicate_date_rejected():
    row = "2026-10-01,1,1,1,1,1,1,1,1,1,9\n"
    assert not import_inverter(csv(INV_HEADER + row + row)).ok


def test_inverter_total_mismatch_is_warning_and_recomputed():
    res = import_inverter(csv(INV_HEADER + "2026-10-01,1,1,1,1,1,1,1,1,1,50\n"))
    assert res.ok and res.warnings
    assert res.df.loc[0, "Total"] == 9


def test_indian_date_format():
    res = import_inverter(csv(INV_HEADER + "13-10-2026,1,1,1,1,1,1,1,1,1,9\n"))
    assert res.ok and res.df.loc[0, "date"] == pd.Timestamp("2026-10-13")


def test_lt_kwh_from_units_and_mf():
    res = import_lt_meter(csv("from_date,to_date,opening,closing\n2026-10-03,2026-10-03,100.0,114.3\n"))
    assert res.ok and res.df.loc[0, "kwh"] == pytest.approx(4290)


def test_lt_closing_below_opening_rejected():
    res = import_lt_meter(csv("from_date,to_date,opening,closing\n2026-10-03,2026-10-03,120,110\n"))
    assert not res.ok


def test_ht_net_computed():
    res = import_ht_meter(csv("date,import_kwh,export_kwh\n2026-10-04,6366,714\n"))
    assert res.ok and res.df.loc[0, "net_import_kwh"] == 5652


def test_blank_or_na_inverter_value_rejected():
    res = import_inverter(csv(INV_HEADER + "2026-10-01,1,1,1,1,n/a,1,1,1,1,9\n"))
    assert not res.ok and any("PV5" in e for e in res.errors)


# ---------- filters ----------

from core.filters import apply_filters, enrich, productivity_band, weather_group


def test_weather_groups():
    assert weather_group("Overcast / Patchy Rain") == "Rain / storm"
    assert weather_group("Partly Cloudy / Mostly Cloudy") == "Partly cloudy"
    assert weather_group("Sunny / Hazy") == "Sunny"
    assert weather_group("Mostly Cloudy") == "Cloudy"
    assert weather_group("") == "Not recorded"


def test_productivity_bands():
    assert productivity_band(0.3) == "Very low"
    assert productivity_band(3.0) == "Normal"
    assert productivity_band(5.2) == "High"


def test_outage_survives_filtering(sample):
    inv, lt, ht = sample
    e = enrich(inv)
    f_inv, _, _ = apply_filters(e, lt, ht, productivity=["Very low"])
    assert f_inv["is_outage"].tolist() == [True]          # 11 Oct only


def test_lt_period_kept_only_if_all_days_kept(sample):
    inv, lt, ht = sample
    e = enrich(inv)
    _, f_lt, f_ht = apply_filters(e, lt, ht, start=pd.Timestamp("2026-10-02"), end=pd.Timestamp("2026-10-05"))
    assert "01–02 Oct" not in f_lt["period"].tolist()     # 01 Oct filtered out
    assert f_lt["period"].tolist() == ["03 Oct", "04 Oct", "05 Oct"]
    assert len(f_ht) == 4


# ---------- AC loss, EB / power factor, customer averages ----------

from core.calculations import ac_loss_totals, ac_losses, customer_averages, grid_summary


def test_ac_loss_normal_days_and_gaps(sample):
    inv, lt, _ = sample
    losses = ac_losses(inv, lt).set_index("period")
    assert losses.loc["03 Oct", "loss_kwh"] == pytest.approx(177.71, abs=0.01)
    assert losses.loc["03 Oct", "status"] == "Normal"
    assert losses.loc["13 Oct", "status"] == "LT higher: inverter data gap"
    t = ac_loss_totals(ac_losses(inv, lt))
    assert t["gap_periods"] == 3
    assert 0.03 < t["clean_loss_pct"] < 0.04


def test_grid_summary_without_pf(sample):
    inv, lt, ht = sample
    g = grid_summary(ht, lt)
    assert g["eb_import_kwh"] == 118908 and g["export_kwh"] == 1872
    assert g["pf_avg"] is None


def test_power_factor_import_and_flag():
    res = import_ht_meter(csv("date,import_kwh,export_kwh,pf\n2026-10-01,100,0,0.95\n2026-10-02,100,0,0.86\n"))
    assert res.ok
    g = grid_summary(res.df, pd.DataFrame({"kwh": [0.0]}))
    assert g["pf_min"] == 0.86 and g["pf_low_days"] == ["02 Oct"]


def test_power_factor_out_of_range_rejected():
    assert not import_ht_meter(csv("date,import_kwh,export_kwh,power_factor\n2026-10-01,100,0,1.4\n")).ok


def test_customer_averages(sample):
    a = customer_averages(*sample)
    assert a["days"] == 13
    assert a["avg_per_inverter_daily"] == pytest.approx(40617.29 / 13 / 9, abs=0.01)
