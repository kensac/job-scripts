from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, List

logger = logging.getLogger("jobtracker_mail")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("JOBTRACKER_MAIL_FROM", "jobtracker@kanishksachdev.com")
APP_URL = os.environ.get("JOBTRACKER_APP_URL", "https://www.kanishksachdev.com/job-tracker")


def configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def send_invite(to: str, invite_url: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "You're invited to Job Tracker"
    msg["From"] = f"Job Tracker <{MAIL_FROM}>"
    msg["To"] = to
    msg.set_content(
        "You've been invited to Job Tracker — an AI-filtered job application tracker.\n\n"
        f"Create your account here (link valid for 7 days):\n{invite_url}\n\n"
        "You'll pick your own username and password; this email address will be your login identity."
    )
    msg.add_alternative(
        f"""<div style="font-family:Inter,system-ui,sans-serif;color:#0c0a08;max-width:560px">
<p>You've been invited to <strong>Job Tracker</strong> — an AI-filtered job application tracker.</p>
<p><a href="{invite_url}" style="color:#533afd">Create your account</a> (link valid for 7 days).</p>
<p style="color:#64748d;font-size:12px">You'll pick your own username and password; this email address will be your login identity.</p>
</div>""",
        subtype="html",
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    logger.info("invite sent")


def send_health_alert(to: str, alerts: List[Dict[str, Any]]) -> None:
    lines = ["Job tracker data-health alerts:", ""]
    html = []
    for a in alerts:
        lines.append(f"- [{a['severity'].upper()}] {a['message']}")
        html.append(
            f"<li><strong>{a['severity'].upper()}</strong> — {a['message']}</li>"
        )
    lines += ["", f"Admin: {APP_URL.replace('/job-tracker', '/job-scripts')}/dashboard"]
    msg = EmailMessage()
    msg["Subject"] = f"Job tracker: {len(alerts)} data-health alert{'s' if len(alerts) != 1 else ''}"
    msg["From"] = f"Job Tracker <{MAIL_FROM}>"
    msg["To"] = to
    msg.set_content("\n".join(lines))
    msg.add_alternative(
        f"""<div style="font-family:Inter,system-ui,sans-serif;color:#0c0a08;max-width:640px">
<p>Something upstream looks different than it did last week:</p>
<ul>{"".join(html)}</ul>
<p style="color:#64748d;font-size:12px">Alerts fire once when a condition starts and
auto-resolve when it stops.</p>
</div>""",
        subtype="html",
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    logger.info(f"health alert mail sent ({len(alerts)} alerts)")


def send_digest(to: str, jobs: List[Dict[str, Any]], unsubscribe_token: str) -> None:
    unsubscribe_url = f"{APP_URL}/unsubscribe?token={unsubscribe_token}"
    count = len(jobs)
    lines = [
        f"{count} new job{'s' if count != 1 else ''} landed on your board.",
        "",
    ]
    html_rows = []
    for j in jobs[:50]:
        loc = ", ".join(j.get("locations") or []) or "—"
        comp = j.get("comp_text") or ""
        lines.append(f"- {j['company']} — {j['title']} ({loc}){' · ' + comp if comp else ''}")
        html_rows.append(
            f"<tr><td style='padding:6px 12px 6px 0'><strong>{j['company']}</strong></td>"
            f"<td style='padding:6px 12px 6px 0'>{j['title']}</td>"
            f"<td style='padding:6px 12px 6px 0;color:#64748d'>{loc}</td>"
            f"<td style='padding:6px 0;color:#64748d'>{comp}</td></tr>"
        )
    if count > 50:
        lines.append(f"…and {count - 50} more.")
        html_rows.append(
            f"<tr><td colspan='4' style='padding:6px 0;color:#64748d'>…and {count - 50} more.</td></tr>"
        )
    lines += ["", f"Open your board: {APP_URL}", "", f"Unsubscribe: {unsubscribe_url}"]

    msg = EmailMessage()
    msg["Subject"] = f"{count} new job{'s' if count != 1 else ''} on your board"
    msg["From"] = f"Job Tracker <{MAIL_FROM}>"
    msg["To"] = to
    msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content("\n".join(lines))
    msg.add_alternative(
        f"""<div style="font-family:Inter,system-ui,sans-serif;color:#0c0a08;max-width:640px">
<p>{count} new job{"s" if count != 1 else ""} landed on your board.</p>
<table style="border-collapse:collapse;font-size:14px">{"".join(html_rows)}</table>
<p style="margin-top:16px"><a href="{APP_URL}" style="color:#533afd">Open your board</a></p>
<p style="color:#64748d;font-size:12px">You get this because email digests are on in your tracker preferences.
<a href="{unsubscribe_url}" style="color:#64748d">Unsubscribe</a></p>
</div>""",
        subtype="html",
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)
    logger.info(f"digest sent to user with {count} jobs")
