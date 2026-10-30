"""Email bot: imports monthly sheets sent to a dedicated mailbox, with no one in the app.

How it works (run on a schedule by .github/workflows/data-bot.yml):
  1. Log in to the bot mailbox over IMAP and read UNREAD emails.
  2. Ignore senders not in ALLOWED_SENDERS.
  3. For each .xlsx/.csv attachment: detect inverter / LT / HT from its columns,
     run the same checks as the app, save to Supabase only if every check passes.
  4. Record every file in the import log, mark the email read, and reply with a report.

Settings (environment variables / GitHub Actions secrets):
  DATABASE_URL, BOT_IMAP_HOST, BOT_SMTP_HOST, BOT_EMAIL, BOT_PASSWORD,
  ALLOWED_SENDERS (comma-separated), BOT_NOTIFY (optional, comma-separated, copied on every reply)
Run by hand:  python -m bot.email_bot
"""
from __future__ import annotations

import email
import imaplib
import smtplib
import sys
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import parseaddr

from bot.detect import NamedBytes, detect_kind
from core import db
from core.importer import import_ht_meter, import_inverter, import_lt_meter
from core.settings import get_setting

KINDS = {
    "inverter": ("Inverter", import_inverter, "save_inverter"),
    "lt": ("LT meter", import_lt_meter, "save_lt"),
    "ht": ("HT meter", import_ht_meter, "save_ht"),
}
# Save in this order so a month's inverter data exists before its meters
ORDER = {"inverter": 0, "lt": 1, "ht": 2, "unknown": 3}


def _csv_setting(name: str) -> set[str]:
    return {x.strip().lower() for x in (get_setting(name, "") or "").split(",") if x.strip()}


def _text(value) -> str:
    return str(make_header(decode_header(value or "")))


def attachments(msg: Message) -> list[NamedBytes]:
    files = []
    for part in msg.walk():
        name = part.get_filename()
        if not name:
            continue
        name = _text(name)
        if name.lower().endswith((".xlsx", ".xlsm", ".csv")):
            files.append(NamedBytes(part.get_payload(decode=True) or b"", name))
    return files


def process_message(msg: Message, allowed: set[str], database=db) -> dict:
    """Validate and save every sheet in one email. Returns a report for the reply."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    report = {"sender": sender, "subject": _text(msg.get("Subject")), "results": [], "accepted": True}
    if allowed and sender not in allowed:
        report["accepted"] = False
        return report

    files = attachments(msg)
    if not files:
        report["results"].append({"file": "(none)", "kind": "-", "status": "skipped", "rows": 0,
                                  "errors": ["No .xlsx or .csv attachment found"], "warnings": []})
    tagged = sorted(((detect_kind(f), f) for f in files), key=lambda t: ORDER[t[0]])
    for kind, f in tagged:
        if kind == "unknown":
            r = {"file": f.name, "kind": "unknown", "status": "skipped", "rows": 0, "warnings": [],
                 "errors": ["Could not tell which sheet this is: column names don't match the inverter, "
                            "LT meter or HT meter template"]}
        else:
            label, importer, saver = KINDS[kind]
            try:
                res = importer(f)
                errors, warnings = res.errors, res.warnings
            except Exception as e:  # unreadable file
                res, errors, warnings = None, [f"Could not read file: {e}"], []
            if res is not None and res.ok:
                getattr(database, saver)(res.df)
                r = {"file": f.name, "kind": label, "status": "saved", "rows": len(res.df),
                     "errors": [], "warnings": warnings}
            else:
                r = {"file": f.name, "kind": label, "status": "refused", "rows": 0,
                     "errors": errors, "warnings": warnings}
        database.log_import("email-bot", sender, r["file"], r["kind"], r["status"], r["rows"],
                            "; ".join(r["errors"] + r["warnings"]))
        report["results"].append(r)
    return report


def reply_text(report: dict) -> str:
    lines = ["SRTPV data bot: import report", ""]
    for r in report["results"]:
        icon = {"saved": "SAVED", "refused": "REFUSED (nothing saved)", "skipped": "SKIPPED"}[r["status"]]
        lines.append(f"- {r['file']}  [{r['kind']}]  {icon}" + (f"  {r['rows']} rows" if r["rows"] else ""))
        lines += [f"    error: {e}" for e in r["errors"]]
        lines += [f"    note: {w}" for w in r["warnings"]]
    if any(r["status"] != "saved" for r in report["results"]):
        lines += ["", "Fix the file(s) marked REFUSED or SKIPPED and email them again. "
                      "Files marked SAVED do not need to be resent."]
    lines += ["", "This is an automatic message."]
    return "\n".join(lines)


def send_reply(report: dict, to: str, cc: set[str]) -> None:
    host = get_setting("BOT_SMTP_HOST")
    user, password = get_setting("BOT_EMAIL"), get_setting("BOT_PASSWORD")
    m = EmailMessage()
    m["From"], m["To"] = user, to
    if cc - {to}:
        m["Cc"] = ", ".join(sorted(cc - {to}))
    status = "OK" if all(r["status"] == "saved" for r in report["results"]) else "ACTION NEEDED"
    m["Subject"] = f"[{status}] Re: {report['subject']}"
    m.set_content(reply_text(report))
    with smtplib.SMTP_SSL(host, int(get_setting("BOT_SMTP_PORT", "465"))) as smtp:
        smtp.login(user, password)
        smtp.send_message(m)


def run() -> int:
    required = ["DATABASE_URL", "BOT_IMAP_HOST", "BOT_SMTP_HOST", "BOT_EMAIL", "BOT_PASSWORD", "ALLOWED_SENDERS"]
    missing = [k for k in required if not get_setting(k)]
    if missing:
        print(f"Missing settings: {missing}")
        return 2
    allowed, notify = _csv_setting("ALLOWED_SENDERS"), _csv_setting("BOT_NOTIFY")
    db.init_db()

    imap = imaplib.IMAP4_SSL(get_setting("BOT_IMAP_HOST"))
    imap.login(get_setting("BOT_EMAIL"), get_setting("BOT_PASSWORD"))
    imap.select("INBOX")
    _, data = imap.search(None, "UNSEEN")
    ids = data[0].split()
    print(f"{len(ids)} unread email(s)")
    for num in ids:
        _, parts = imap.fetch(num, "(BODY.PEEK[])")       # PEEK: stays unread if we crash
        msg = email.message_from_bytes(parts[0][1])
        report = process_message(msg, allowed)
        if not report["accepted"]:
            print(f"Ignored email from {report['sender']} (not in ALLOWED_SENDERS)")
        else:
            for r in report["results"]:
                print(f"{report['sender']}: {r['file']} -> {r['kind']} {r['status']} {r['rows']} rows")
            send_reply(report, report["sender"], notify)
        imap.store(num, "+FLAGS", "\\Seen")
    imap.logout()
    return 0


if __name__ == "__main__":
    sys.exit(run())
