"""Email bot tests. The database is replaced by a recorder, so no Supabase is needed."""
import io
import sys
import types
from email.message import EmailMessage
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample"


class FakeDB:
    def __init__(self):
        self.saved, self.logs = [], []

    def save_inverter(self, df): self.saved.append(("inverter", len(df)))
    def save_lt(self, df): self.saved.append(("lt", len(df)))
    def save_ht(self, df): self.saved.append(("ht", len(df)))
    def log_import(self, *a): self.logs.append(a)


@pytest.fixture(scope="module")
def bot():
    if "core.db" not in sys.modules:                 # avoid needing SQLAlchemy for these tests
        sys.modules["core.db"] = types.ModuleType("core.db")
    from bot import email_bot
    return email_bot


def _mail(sender, files):
    m = EmailMessage()
    m["From"], m["Subject"] = sender, "readings"
    m.set_content("attached")
    for name, data in files:
        m.add_attachment(data, maintype="text", subtype="csv", filename=name)
    return m


def _csv(name):
    return (SAMPLE / name).read_bytes()


def test_detects_files_by_columns_not_names(bot):
    from bot.detect import NamedBytes, detect_kind
    assert detect_kind(NamedBytes(_csv("inverter_october_2026.csv"), "x.csv")) == "inverter"
    assert detect_kind(NamedBytes(_csv("lt_meter_october_2026.csv"), "y.csv")) == "lt"
    assert detect_kind(NamedBytes(_csv("ht_meter_october_2026.csv"), "z.csv")) == "ht"
    assert detect_kind(NamedBytes(b"name,amount\na,1\n", "w.csv")) == "unknown"


def test_saves_valid_month_in_order(bot):
    fake = FakeDB()
    m = _mail("site@plant.in", [("c.csv", _csv("ht_meter_october_2026.csv")),
                                ("a.csv", _csv("inverter_october_2026.csv")),
                                ("b.csv", _csv("lt_meter_october_2026.csv"))])
    rep = bot.process_message(m, {"site@plant.in"}, database=fake)
    assert [k for k, _ in fake.saved] == ["inverter", "lt", "ht"]
    assert all(r["status"] == "saved" for r in rep["results"]) and len(fake.logs) == 3


def test_refuses_bad_file_and_saves_nothing_from_it(bot):
    fake = FakeDB()
    bad = b"from_date,to_date,opening,closing\n2026-10-03,2026-10-03,120,110\n"
    rep = bot.process_message(_mail("site@plant.in", [("lt.csv", bad)]), {"site@plant.in"}, database=fake)
    assert fake.saved == [] and rep["results"][0]["status"] == "refused"


def test_ignores_unknown_sender(bot):
    fake = FakeDB()
    rep = bot.process_message(_mail("someone@else.com", [("a.csv", _csv("inverter_october_2026.csv"))]),
                              {"site@plant.in"}, database=fake)
    assert not rep["accepted"] and fake.saved == [] and fake.logs == []
