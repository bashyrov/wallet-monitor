"""Minimal SMTP mailer.

Single concern: send a plain-text email to a single recipient using
whatever SMTP relay is configured via env vars. Works with any provider
that exposes SMTP (Gmail, SES, Mailgun, Postmark, Resend, etc.).

ENV:
    SMTP_HOST       e.g. smtp.resend.com / smtp.mailgun.org
    SMTP_PORT       587 (STARTTLS) or 465 (SSL). default: 587
    SMTP_USER       username for SMTP AUTH (e.g. "resend", api key, or full email)
    SMTP_PASS       password / API token for SMTP AUTH
    SMTP_FROM       From: header (e.g. "Avalant <no-reply@avalant.xyz>")
    SMTP_TLS        "1" (STARTTLS, default), "0" (plain), "ssl" (SMTPS on 465)

If SMTP_HOST is unset, `is_configured()` returns False and `send()` raises
RuntimeError. Callers should check first and fall back to returning the
confirmation code in the response body (dev-only via
AVALANT_AUTH_DEV_EXPOSE_TOKEN=1).
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger("avalant.mailer")


def is_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def _smtp_from() -> str:
    src = (os.environ.get("SMTP_FROM") or "").strip()
    return src or "Avalant <no-reply@avalant.xyz>"


def send(to: str, subject: str, body: str) -> None:
    """Send a plain-text email. Raises RuntimeError if mailer is not
    configured or SMTP returns an error. Caller is responsible for
    catching and falling back."""
    if not is_configured():
        raise RuntimeError("SMTP not configured (set SMTP_HOST)")
    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = (os.environ.get("SMTP_USER") or "").strip()
    pw = os.environ.get("SMTP_PASS") or ""
    mode = (os.environ.get("SMTP_TLS") or "1").lower()

    msg = EmailMessage()
    msg["From"] = _smtp_from()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    ctx = ssl.create_default_context()
    try:
        if mode == "ssl":
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
                if user and pw:
                    s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                if mode == "1":
                    s.starttls(context=ctx)
                    s.ehlo()
                if user and pw:
                    s.login(user, pw)
                s.send_message(msg)
        logger.info("mailer: sent to=%s subject=%r", to, subject)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mailer: send failed to=%s err=%s", to, exc)
        raise
