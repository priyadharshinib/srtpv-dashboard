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
