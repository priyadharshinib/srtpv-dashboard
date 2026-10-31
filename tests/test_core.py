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


def test_blank_or_na_inverter_value_stays_blank_in_actuals():
    res = import_inverter(csv(INV_HEADER + "2026-10-01,1,1,1,1,n/a,1,1,1,1,9\n"
                                         "2026-10-02,1,1,1,1,1,1,1,1,1,9\n"))
    assert res.ok and pd.isna(res.df.loc[0, "PV5"]) and res.df.loc[0, "Total"] == 8
    assert any("blank" in w for w in res.warnings)


def test_text_in_inverter_column_still_rejected():
    assert not import_inverter(csv(INV_HEADER + "2026-10-01,1,1,1,1,abc,1,1,1,1,9\n")).ok


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


# ---------- Actuals vs Projected view ----------

from core.config import INVERTER_IDS
from core.projection import project
from core.views import ACTUALS, PROJECTED, apply_view, views_for


def test_role_views(monkeypatch):
    monkeypatch.setattr("core.views.get_setting", lambda *a, **k: None)
    assert views_for("admin") == [ACTUALS, PROJECTED]
    assert views_for("site_manager") == [ACTUALS]
    assert views_for("customer") == [PROJECTED]


def test_projected_view_can_be_switched_off(monkeypatch):
    monkeypatch.setattr("core.views.get_setting", lambda *a, **k: "false")
    assert views_for("customer") == [ACTUALS] and views_for("admin") == [ACTUALS]


def _month(pv4_values):
    """A month of days: eight healthy inverters at 300 kWh, PV4 as given."""
    rows = []
    for d, v in enumerate(pv4_values, 1):
        row = {"date": pd.Timestamp(2026, 9, d), **{p: 300.0 for p in INVERTER_IDS}}
        row["PV4"] = v
        rows.append(row)
    inv = pd.DataFrame(rows)
    inv["Total"] = inv[INVERTER_IDS].sum(axis=1)
    return inv


def test_projection_keeps_day_and_month_totals():
    inv = _month([0.0] * 20 + [300.0] * 10)                       # PV4 down on 20 of 30 days
    out, marked = project(inv)
    assert (out[INVERTER_IDS].sum(axis=1).round(2) == inv["Total"].round(2)).all()
    assert round(out["Total"].sum(), 2) == round(inv["Total"].sum(), 2)
    assert out.loc[0, "PV4"] == round(0.1 * 2400, 2) and out.loc[0, "PV1"] == 270.0
    assert marked["PV4"].all() and not marked["PV1"].any()


def test_projection_matches_september_report_rule():
    # 03 Sep 2026 actual readings; the report shows PV1 222.91, PV4 216.89, PV9 254.00
    row = {"date": pd.Timestamp("2026-09-03"), "PV1": 247.68, "PV2": 254.58, "PV3": 258.49, "PV4": 0.0,
           "PV5": 288.44, "PV6": 289.22, "PV7": 274.87, "PV8": 273.43, "PV9": 282.22}
    inv = pd.concat([_month([0.0] * 30).iloc[:29], pd.DataFrame([row])], ignore_index=True)
    inv["Total"] = inv[INVERTER_IDS].sum(axis=1)
    out, _ = project(inv)
    r = out.iloc[-1]
    assert (r["PV1"], r["PV4"], r["PV9"]) == (222.91, 216.89, 254.0)
    assert round(r[INVERTER_IDS].sum(), 2) == 2168.93


def test_month_without_chronic_fault_is_unchanged():
    inv = _month([0.0] * 5 + [300.0] * 25)                         # PV4 down on only 5 days
    out, marked = project(inv)
    assert out[INVERTER_IDS].equals(inv[INVERTER_IDS]) and not marked.values.any()


def test_projection_leaves_whole_offline_group_blank():
    row = {"date": pd.Timestamp("2026-07-29"), **{p: 150.0 for p in INVERTER_IDS[:6]},
           "PV7": float("nan"), "PV8": float("nan"), "PV9": float("nan")}
    out, _ = project(pd.DataFrame([row]))
    assert round(out[INVERTER_IDS].sum(axis=1).iloc[0], 2) == 900.0


def test_projected_view_changes_only_inverter_columns(sample):
    inv, lt, ht = sample
    pv = apply_view(inv, PROJECTED)
    other = [c for c in inv.columns if c not in INVERTER_IDS + ["Total"]]
    pd.testing.assert_frame_equal(pv[other], inv[other])
    assert pv["PV4"].sum() > inv["PV4"].sum()                          # PV4 was weak in October
    assert round(pv["Total"].sum(), 2) == round(inv["Total"].sum(), 2)  # totals never change
    assert pv[[f"proj_{p}" for p in INVERTER_IDS]].values.sum() > 0
    assert apply_view(inv, ACTUALS)[INVERTER_IDS].equals(inv[INVERTER_IDS])


def test_glance_headline_names_inverter_only_in_actuals(sample):
    from core.observations import glance_headline
    inv, lt, ht = sample
    k = headline_kpis(inv, lt, ht)
    actual = glance_headline(k, inv)
    assert actual.startswith("Solar covered 27% of site consumption")
    assert "PV4" in actual and "11 Oct" in actual
    assert "PV4" not in glance_headline(k, apply_view(inv, PROJECTED))


# ---------- HT bill ----------

from core.billing import demand_headline, demand_pct, demand_story, inr


def test_inr_indian_grouping():
    assert inr(2979191) == "₹29,79,191"
    assert inr(74664) == "₹74,664"
    assert inr(175490) == "₹1,75,490"
    assert inr(950) == "₹950"


def test_demand_headline_and_story_match_september_bill():
    dates = pd.date_range("2026-09-02", "2026-09-30")
    inv = pd.DataFrame({"date": dates, "Total": 3136.14, "weather": "Partly Cloudy"})
    rain = inv["date"] == pd.Timestamp("2026-09-29")
    inv.loc[rain, "Total"] = 494.09
    inv.loc[rain, "weather"] = "Moderate Rain"
    bill = {"max_demand_kva": 1195.8, "contract_demand_kva": 1200, "md_at": pd.Timestamp("2026-09-29 10:30")}
    assert demand_pct(1195.8, 1200) == 99.6
    assert demand_headline(bill, inv) == ("Maximum demand came within 0.4% of contract demand, "
                                          "on the month's weakest solar day")
    assert demand_story(bill, inv, "September") == (
        "The 1,195.8 kVA peak was set at 10:30 on 29 Sep. Rain held the inverters to 494 kWh that day, "
        "against a September median of about 3,136 kWh, so almost the whole morning load came from the grid.")



# ---------- missing readings, bill spreading, report layout ----------

from core.estimation import bill_daily, fill_missing_inverters
from core.report import build_period, daily_headline, short_weather


def _sep_real():
    """September 2026 as in the AECIPL monthly report (02-30 Sep inverter totals; 01 Sep no data)."""
    totals = [2583.28, 2168.93, 1833.12, 4079.12, 1924.93, 5224.95, 4890.78, 3051.03, 2727.96, 3877.02, 4633.92,
              3517.26, 2173.51, 3619.27, 5283.56, 4161.66, 2502.68, 4499.61, 3322.87, 1005.29, 4241.12, 3053.50,
              3806.66, 3136.14, 3089.21, 2305.96, 4541.35, 494.09, 2461.08]
    dates = pd.date_range("2026-09-02", "2026-09-30")
    inv = pd.DataFrame({"date": dates, "Total": totals})
    exp = {5: 213, 6: 269, 12: 242, 13: 492, 19: 234, 20: 466, 26: 161, 27: 323}
    ht = pd.DataFrame({"date": pd.date_range("2026-09-01", "2026-09-30")})
    ht["export_kwh"] = ht["date"].dt.day.map(exp).fillna(0.0)
    ht["power_factor"] = float("nan")
    bill = {"lt_kwh": 304.3 * 300, "ht_import_kwh": 307494, "ht_export_kwh": 2400}
    return inv, ht, bill


def test_bill_spread_reproduces_report():
    inv, ht, bill = _sep_real()
    d = bill_daily(inv, ht, bill, "2026-09").set_index(pd.date_range("2026-09-01", "2026-09-30").day)
    assert round(d.loc[1, "lt_kwh"]) == 3043 and round(d.loc[1, "inverter_est"]) == 3249
    assert round(d.loc[2, "lt_kwh"]) == 2420 and round(d.loc[2, "ht_import_kwh"]) == 11623
    assert round(d.loc[5, "site_load_kwh"]) == 11896 and round(d.loc[5, "ht_import_kwh"]) == 8288
    assert round(d["lt_kwh"].sum()) == 91290 and round(d["ht_import_kwh"].sum()) == 307494
    assert round(d["site_load_kwh"].sum()) == 396384


def test_fill_missing_matches_day_total():
    rows = [{"date": pd.Timestamp(2026, 9, d), **{p: 100.0 + i for i, p in enumerate(INVERTER_IDS)}} for d in (1, 2, 3)]
    inv = pd.DataFrame(rows)
    sheet_total = inv[INVERTER_IDS].sum(axis=1)
    inv.loc[1, "PV3"] = float("nan")
    inv.loc[2, INVERTER_IDS] = float("nan")
    sheet_total[2] = float("nan")
    filled, nodata, notes = fill_missing_inverters(inv, sheet_total)
    assert filled.loc[1, "PV3"] == 102.0 and bool(filled.loc[1, "est_PV3"])
    assert nodata == [pd.Timestamp(2026, 9, 3)] and len(filled) == 2


def test_report_headline_and_weather_labels():
    inv, ht, bill = _sep_real()
    inv["weather"] = "Cloudy"
    inv.loc[inv["date"].dt.day.isin([21, 29]), "weather"] = "Cloudy / Rain"
    assert daily_headline(inv, [pd.Timestamp("2026-09-01")]) == (
        "Best days were 16 and 07 Sep; rain on 21 and 29 Sep cut output sharply. 01 Sep has no inverter data.")
    assert short_weather("Partly cloudy / Mainly sunny") == "Sunny spells"
    assert short_weather("Light Rain / Thunderstorm") == "Thunder"
    assert short_weather("Scattered Clouds / Partly Cloudy") == "Partly cloudy"


def test_build_period_keeps_no_data_day_in_bill_totals():
    inv, ht, bill = _sep_real()
    for c, v in (("day", ""), ("weather", ""), ("temp_c", float("nan")), ("wind_ms", float("nan"))):
        inv[c] = v
    lt = pd.DataFrame({c: pd.Series(dtype="datetime64[ns]" if "date" in c else float)
                       for c in ["from_date", "to_date", "kwh", "opening", "closing", "units"]})
    ht2 = ht.assign(import_kwh=0.0, net_import_kwh=0.0)
    l, h, daily, nodata, bm = build_period(inv, lt, ht2, {"2026-09": bill},
                                           pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-30"))
    assert bm == ["2026-09"] and nodata == [pd.Timestamp("2026-09-01")] and len(daily) == 30
    assert round(l["kwh"].sum()) == 91290 and round(h["import_kwh"].sum()) == 307494


# ---------- importing a month in parts ----------

from core.batches import ADD_NEW, REPLACE, prepare_batch, ranges


def test_second_half_of_month_is_appended():
    first = pd.DataFrame({"date": pd.date_range("2026-10-01", "2026-10-15")})
    second = pd.DataFrame({"date": pd.date_range("2026-10-14", "2026-10-31")})
    df, errors, notes = prepare_batch("inverter", second, "2026-10", list(first["date"]), ADD_NEW)
    assert not errors and len(df) == 18 - 2 and df["date"].min() == pd.Timestamp("2026-10-16")
    df, errors, notes = prepare_batch("inverter", second, "2026-10", list(first["date"]), REPLACE)
    assert len(df) == 18 and any("Replaced 2" in n for n in notes)


def test_rows_outside_chosen_month_are_refused():
    df = pd.DataFrame({"date": pd.date_range("2026-09-30", "2026-10-02")})
    saved, errors, _ = prepare_batch("ht", df, "2026-10", [])
    assert saved.empty and "outside October 2026" in errors[0]


def test_lt_batch_checks_continuity():
    lt = pd.DataFrame({"from_date": pd.to_datetime(["2026-10-16"]), "to_date": pd.to_datetime(["2026-10-16"]),
                       "opening": [120.0], "closing": [130.0]})
    _, _, notes = prepare_batch("lt", lt, "2026-10", [], last_lt_closing=118.0)
    assert any("does not match" in n for n in notes)


def test_ranges_text():
    days = list(pd.date_range("2026-10-01", "2026-10-03")) + [pd.Timestamp("2026-10-05")]
    assert ranges(days) == "01–03 Oct, 05 Oct"


def test_customer_headline_has_no_outage_clause(sample):
    from core.observations import glance_headline
    inv, lt, ht = sample
    k = headline_kpis(inv, lt, ht)
    assert "largely off" in glance_headline(k, inv)
    assert "largely off" not in glance_headline(k, apply_view(inv, PROJECTED), include_outages=False)



# ---------- portal export in long format (one row per inverter per day) ----------

LONG = ("Date,Roof,Temprature,Weather,Wind Speed,Production,Consumption,Daily Total\n"
        "[dd.MM.yyyy],[RoofName],[degree C],[Condition],[m/s],[kWh],[kWh],[kWh]\n")


def _long_day(day, values, labels=None):
    labels = labels or INVERTER_IDS
    rows = [f"{day},RR roof {lab},25°C,Cloudy,4.0 m/s,{v},{v}," for lab, v in zip(labels, values)]
    return "\n".join(rows) + "\n"


def test_long_format_is_read():
    res = import_inverter(csv(LONG + _long_day("01.10.2026", [100 + i for i in range(9)])))
    assert res.ok and len(res.df) == 1 and res.df.loc[0, "PV9"] == 108 and res.df.loc[0, "temp_c"] == 25


def test_long_format_mislabelled_row_is_corrected():
    labels = ["PV1", "PV2", "PV3", "PV4", "PV5", "PV6", "PV6", "PV8", "PV9"]   # PV7 written as PV6
    res = import_inverter(csv(LONG + _long_day("03.09.2026", [1, 2, 3, 0, 5, 6, 7, 8, 9], labels)))
    assert res.ok and res.df.loc[0, "PV7"] == 7 and res.df.loc[0, "PV6"] == 6
    assert any("Corrected" in w for w in res.warnings)


def test_long_format_ambiguous_duplicate_is_refused():
    labels = INVERTER_IDS + ["PV5"]                                   # ten rows: nothing is missing
    res = import_inverter(csv(LONG + _long_day("03.09.2026", list(range(10)), labels)))
    assert not res.ok and "appears twice" in res.errors[0]


def test_build_period_mixed_months_keeps_date_types():
    inv, ht, bill = _sep_real()
    for c, v in (("day", ""), ("weather", ""), ("temp_c", float("nan")), ("wind_ms", float("nan"))):
        inv[c] = v
    aug = pd.DataFrame({"date": pd.date_range("2026-08-01", "2026-08-05"), "Total": 3000.0, "day": "",
                        "weather": "", "temp_c": float("nan"), "wind_ms": float("nan")})
    lt = pd.DataFrame(columns=["period", "from_date", "to_date", "opening", "closing", "units", "kwh", "note"])
    ht2 = ht.assign(import_kwh=0.0, net_import_kwh=0.0)
    l, h, daily, _, bm = build_period(pd.concat([aug, inv], ignore_index=True), lt, ht2, {"2026-09": bill},
                                      pd.Timestamp("2026-08-01"), pd.Timestamp("2026-09-30"))
    assert str(l["from_date"].dtype).startswith("datetime64") and str(h["date"].dtype).startswith("datetime64")
    assert bm == ["2026-09"] and round(l["kwh"].sum()) == 91290


def test_projection_share_is_per_month():
    # 01 Oct 2026 actual readings; the October report shows PV1 348.93 and PV4 225.65 (7% share)
    row = {"date": pd.Timestamp("2026-10-01"), "PV1": 375.19, "PV2": 379.6, "PV3": 376.82, "PV4": 0.0,
           "PV5": 429.04, "PV6": 435.9, "PV7": 403.88, "PV8": 401.55, "PV9": 421.69}
    inv = pd.DataFrame([row] * 3)
    inv["date"] = pd.date_range("2026-10-01", periods=3)
    inv["Total"] = inv[INVERTER_IDS].sum(axis=1)
    out, _ = project(inv, {"2026-10": 0.07})                         # share saved in the app for October
    assert (out.loc[0, "PV1"], out.loc[0, "PV4"]) == (348.93, 225.65)
    out2, _ = project(inv)                                           # no saved share -> default 10%
    assert out2.loc[0, "PV1"] == round(375.19 * 0.9, 2)
