"""Work out which sheet a file is from its column names, so file names don't matter."""
from __future__ import annotations

import io

from core.config import INVERTER_IDS
from core.importer import read_table


class NamedBytes(io.BytesIO):
    """Bytes with a .name, so core.importer.read_table can pick CSV or Excel."""

    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


def detect_kind(file: NamedBytes) -> str:
    """Return "inverter", "lt", "ht" or "unknown"."""
    try:
        cols = set(read_table(file).columns)
    except Exception:
        return "unknown"
    finally:
        file.seek(0)
    if set(INVERTER_IDS) <= cols:
        return "inverter"
    if {"from_date", "opening", "closing"} <= cols:
        return "lt"
    if {"import_kwh", "export_kwh"} <= cols or {"import_units", "export_units"} <= cols:
        return "ht"
    return "unknown"
