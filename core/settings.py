"""One place to read settings.

Order: environment variable -> .env file -> .streamlit/secrets.toml
(on Streamlit Community Cloud, the Secrets box fills st.secrets).
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def get_setting(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    try:
        import streamlit as st

        value = st.secrets.get(name)
    except Exception:  # no secrets file, or not running under Streamlit
        value = None
    return value or default
