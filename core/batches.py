"""Importing a month in parts (e.g. 01-15 Oct now, 16-31 Oct later).

prepare_batch() takes a validated sheet, the month it belongs to and the days
already saved for that month, and decides what to save:
  - rows outside the chosen month   -> error, nothing saved
  - days already imported           -> skipped ("add only new days")
                                       or replaced ("replace with this file")
  - LT meter: the new batch's first opening must match the last saved closing
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

ADD_NEW = "Keep what's already there, add only new days"
REPLACE = "Replace those days with this file"


def ranges(days) -> str:
    """[1,2,3,5] Oct -> '01–03 Oct, 05 Oct'."""
    ds = sorted({pd.Timestamp(d).normalize() for d in days})
    if not ds:
        return "none"
    out, start, prev = [], ds[0], ds[0]
    for d in ds[1:] + [None]:
        if d is not None and d - prev == pd.Timedelta(days=1):
            prev = d
            continue
        out.append(f"{start:%d %b}" if start == prev else f"{start:%d}–{prev:%d %b}")
        if d is not None:
            start = prev = d
    return ", ".join(out)


def month_days(month: str) -> list[pd.Timestamp]:
    start = pd.Timestamp(month + "-01")
    return list(pd.date_range(start, start + pd.offsets.MonthEnd(0)))


def _row_days(kind: str, df: pd.DataFrame) -> list[list[pd.Timestamp]]:
    if kind == "lt":
        return [list(pd.date_range(a, b)) for a, b in zip(df["from_date"], df["to_date"])]
    return [[pd.Timestamp(d)] for d in df["date"]]


def prepare_batch(kind: str, df: pd.DataFrame, month: str, existing_days, mode: str = ADD_NEW,
                  last_lt_closing: float | None = None):
    """Return (df_to_save, errors, notes). kind is 'inverter', 'lt' or 'ht'."""
    errors, notes = [], []
    in_month = set(month_days(month))
    rows = _row_days(kind, df)
    outside = sorted({d for days in rows for d in days if d not in in_month})
    if outside:
        errors.append(f"The file has dates outside {pd.Timestamp(month + '-01'):%B %Y}: {ranges(outside)}. "
                      "Choose the right month, or remove those rows.")
        return df.iloc[0:0], errors, notes

    existing = {pd.Timestamp(d).normalize() for d in existing_days}
    overlap = [any(d in existing for d in days) for days in rows]
    if any(overlap):
        dup_days = sorted({d for days, o in zip(rows, overlap) if o for d in days if d in existing})
        if mode == ADD_NEW:
            notes.append(f"Skipped {len(dup_days)} day(s) already imported: {ranges(dup_days)}")
            df = df[[not o for o in overlap]]
        else:
            notes.append(f"Replaced {len(dup_days)} day(s) already imported: {ranges(dup_days)}")

    if kind == "lt" and len(df) and last_lt_closing is not None:
        first_open = float(df.sort_values("from_date")["opening"].iloc[0])
        if abs(first_open - last_lt_closing) > 0.05:
            notes.append(f"LT opening {first_open:.1f} does not match the last saved closing "
                         f"{last_lt_closing:.1f}; check the readings")
    if len(df):
        notes.append(f"Adding {ranges(d for days in _row_days(kind, df) for d in days)}")
    return df.reset_index(drop=True), errors, notes


def coverage_text(month: str, cov: dict) -> dict[str, str]:
    """{'inverter': '01–15 Oct (15 of 31 days) · still to come: 16–31 Oct', ...}"""
    days = month_days(month)
    out = {}
    for kind in ("inverter", "lt", "ht"):
        have = {pd.Timestamp(d) for d in cov.get(kind, [])}
        if not have:
            out[kind] = "nothing imported yet"
            continue
        missing = [d for d in days if d not in have and d <= max(pd.Timestamp(dt.date.today()), max(have))]
        text = f"{ranges(have)} ({len(have)} of {len(days)} days)"
        if missing:
            text += f" · not yet in: {ranges(missing)}"
        out[kind] = text
    return out
