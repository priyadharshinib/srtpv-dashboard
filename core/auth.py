"""Login with Supabase Auth (email + password) and app roles.

Roles (internal key -> name shown in the app):
  admin         - Asase O&M Manager: dashboard in Actuals or Avg view, import, users, notes
  site_manager  - Asase O&M Executive: O&M dashboard (Actuals), import
  customer      - Customer: dashboard in Avg view only (see core/views.py)
Anyone who can log in to Supabase but has no role gets no access.
"""
from __future__ import annotations

import requests

from core import db
from core.settings import get_setting

ROLES = ["admin", "site_manager", "customer"]
ROLE_LABELS = {
    "admin": "Asase O&M Manager",
    "site_manager": "Asase O&M Executive",
    "customer": "Customer",
}


class AuthError(Exception):
    pass


def sign_in(email: str, password: str) -> dict:
    url = get_setting("SUPABASE_URL")
    key = get_setting("SUPABASE_ANON_KEY")
    if not url or not key:
        raise AuthError("Login is not set up: add SUPABASE_URL and SUPABASE_ANON_KEY to the secrets.")
    try:
        r = requests.post(
            f"{url.rstrip('/')}/auth/v1/token?grant_type=password",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"email": email.strip(), "password": password},
            timeout=15,
        )
    except requests.RequestException as e:
        raise AuthError(f"Could not reach Supabase: {e}") from e
    if r.status_code != 200:
        try:
            data = r.json()
        except ValueError:
            data = {}
        msg = data.get("error_description") or data.get("msg") or data.get("message") or "Login failed"
        raise AuthError(msg)
    user = r.json()["user"]
    return {"email": user["email"].lower(), "user_id": user["id"]}


def admin_emails() -> set[str]:
    """Emails listed in the ADMIN_EMAILS secret are always admins (bootstraps the first admin)."""
    raw = get_setting("ADMIN_EMAILS", "") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def role_for(email: str) -> str | None:
    email = email.lower()
    if email in admin_emails():
        return "admin"
    return db.get_user_role(email)
