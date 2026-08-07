"""Transactional email via Resend (verification + password reset).

If resend_api_key is empty (local dev / no provider yet) sending is a no-op that
just logs — the app keeps working, links are only reachable if the user clicks a
logged URL. Wire a real key in cloud for actual delivery.
"""
from __future__ import annotations

import logging
from html import escape as html_escape

import httpx

from config import get_settings

log = logging.getLogger(__name__)
_RESEND_URL = "https://api.resend.com/emails"


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send one email. Returns True if actually dispatched, False if skipped/failed."""
    s = get_settings()
    if not s.resend_api_key:
        log.info("Email skipped (no RESEND_API_KEY): to=%s subject=%r", to, subject)
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _RESEND_URL,
                headers={"Authorization": f"Bearer {s.resend_api_key}"},
                json={"from": s.email_from, "to": [to], "subject": subject, "html": html},
            )
        if r.status_code >= 400:
            log.warning("Resend send failed (%s): %s", r.status_code, r.text[:300])
            return False
        return True
    except httpx.RequestError as e:
        log.warning("Resend request error: %s", e)
        return False


def _link(path: str, token: str) -> str:
    base = (get_settings().public_base_url or "").rstrip("/")
    return f"{base}{path}?token={token}"


async def send_verify_email(to: str, token: str) -> bool:
    url = _link("/verify", token)
    html = (
        f"<p>Welcome to Content Engine!</p>"
        f"<p>Confirm your email to finish setting up your account:</p>"
        f'<p><a href="{url}">Verify my email</a></p>'
        f"<p>If you didn't sign up, ignore this message.</p>"
    )
    return await send_email(to, "Verify your email · Content Engine", html)


async def send_publish_failed_email(to: str, topic: str, reason: str) -> bool:
    """Tell the owner a scheduled post never went out.

    Only sent once the retries are spent: nobody is watching a scheduled publish,
    so without this the post is silently absent until someone happens to look.
    Intermediate retries stay quiet — three emails about one post is spam.
    """
    base = (get_settings().public_base_url or "").rstrip("/")
    where = f'<p><a href="{base}/">Open Content Engine</a></p>' if base else ""
    html = (
        f"<p>A scheduled post did not publish.</p>"
        f"<p><b>{html_escape(topic)}</b></p>"
        f"<p>Reason: {html_escape(reason)}</p>"
        f"<p>It's marked failed in the app, where you can fix and republish it.</p>"
        f"{where}"
    )
    return await send_email(to, "A scheduled post failed to publish · Content Engine", html)


async def send_connection_broken_email(to: str, platform: str, reason: str) -> bool:
    """Tell the owner a publishing connection stopped working.

    Sent once, on the transition from working to broken — a token that has been
    dead for a week is not news, and a daily reminder just trains the spam filter.
    """
    base = (get_settings().public_base_url or "").rstrip("/")
    where = f'<p><a href="{base}/">Open Connections</a></p>' if base else ""
    label = "Instagram" if platform == "instagram" else platform.upper()
    html = (
        f"<p>Your {html_escape(label)} connection stopped working, so scheduled "
        f"posts to it will fail until it's fixed.</p>"
        f"<p>Reason: {html_escape(reason)}</p>"
        f"<p>Re-check the keys under Connections and use “Test connection”.</p>"
        f"{where}"
    )
    return await send_email(
        to, f"{label} connection needs attention · Content Engine", html)


async def send_reset_email(to: str, token: str) -> bool:
    url = _link("/reset", token)
    html = (
        f"<p>Reset your Content Engine password:</p>"
        f'<p><a href="{url}">Choose a new password</a></p>'
        f"<p>This link expires in 1 hour. If you didn't request it, ignore this message.</p>"
    )
    return await send_email(to, "Reset your password · Content Engine", html)


async def send_team_invite_email(to: str, token: str, owner_email: str) -> bool:
    """Invite somebody to an agency's team.

    Says plainly that accepting grants nothing yet. The alternative — a warm
    welcome to a workspace they then cannot open — turns our own half-built
    feature into their support ticket.
    """
    url = _link("/team/accept", token)
    html = (
        f"<p><b>{owner_email}</b> invited you to their team on Content Engine.</p>"
        f'<p><a href="{url}">Accept the invitation</a></p>'
        f"<p>Accepting records that you're on their team. It does <b>not</b> yet "
        f"give you access to their brands, posts or keys — shared access is "
        f"still being built, and we'll email you when it arrives.</p>"
        f"<p>If you weren't expecting this, ignore the message.</p>"
    )
    return await send_email(to, "You've been invited to a team · Content Engine", html)
