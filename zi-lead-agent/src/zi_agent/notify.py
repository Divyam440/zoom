"""Daily summary to YOU (Slack incoming webhook or email to your own address).

This is a status note for the tool's owner, not outreach: it contains counts
and pacing only, never contact names, emails or phone numbers. Secrets (the
webhook URL, SMTP password) come from environment variables / .env.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage


class NotifyError(RuntimeError):
    pass


def build_summary(*, title: str, lines: list[str]) -> str:
    return "\n".join([title, ""] + [f"- {ln}" for ln in lines])


def send_slack(text: str, webhook_env: str) -> None:
    url = os.environ.get(webhook_env, "")
    if not url.startswith("https://hooks.slack.com/"):
        raise NotifyError(f"{webhook_env} is not set to a Slack incoming-webhook URL")
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                raise NotifyError(f"Slack returned HTTP {resp.status}")
    except OSError as e:  # never include the URL in the message
        raise NotifyError(f"Slack request failed: {type(e).__name__}") from None


def send_email(subject: str, text: str, cfg: dict) -> None:
    host = cfg.get("smtp_host")
    to_addr = cfg.get("to_addr")
    from_addr = cfg.get("from_addr") or to_addr
    if not host or not to_addr:
        raise NotifyError("notify.email.smtp_host and notify.email.to_addr must be set")
    user = os.environ.get(cfg.get("username_env", "ZI_SMTP_USERNAME"), "")
    password = os.environ.get(cfg.get("password_env", "ZI_SMTP_PASSWORD"), "")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, from_addr, to_addr
    msg.set_content(text)
    try:
        with smtplib.SMTP(host, int(cfg.get("smtp_port", 587)), timeout=20) as smtp:
            if cfg.get("use_tls", True):
                smtp.starttls(context=ssl.create_default_context())
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise NotifyError(f"email failed: {type(e).__name__}") from None


def send_summary(notify_cfg: dict, subject: str, text: str) -> str:
    """Returns a short status string. channel: none | slack | email."""
    channel = str(notify_cfg.get("channel") or "none").lower()
    if channel == "none":
        return "not configured"
    if channel == "slack":
        send_slack(text, notify_cfg.get("slack_webhook_env", "ZI_SLACK_WEBHOOK_URL"))
        return "sent to Slack"
    if channel == "email":
        send_email(subject, text, notify_cfg.get("email") or {})
        return "sent by email"
    raise NotifyError(f"unknown notify.channel '{channel}'")
