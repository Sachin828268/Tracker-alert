"""
whatsapp_client.py
~~~~~~~~~~~~~~~~~~
Best-effort, per-user WhatsApp-channel forwarding for "back in stock" alerts.

This module's ONLY job is a DB lookup + one lightweight HTTP POST (via httpx,
already a dependency — nothing new). The actual WhatsApp Web browser
automation lives entirely in the separate whatsapp_forwarder/ service (its
own process/container, its own requirements.txt) — Playwright and its browser
binary are NOT dependencies of this module or of the main bot process at all.

forward_alert() is designed to be scheduled via asyncio.create_task() and
NEVER awaited inline in an alert-sending path. Every failure mode (feature not
configured, user has no active channel, forwarder unreachable, timeout, bad
response) is caught internally and logged — nothing here ever raises into the
caller, and nothing here can delay or block a Telegram send, matching the same
fail-safe principle as affiliate.py's URL conversion.
"""

import html as html_module
import logging
import re

import httpx

from config import WHATSAPP_FORWARDER_URL, WHATSAPP_FORWARDER_SECRET
from database import get_active_whatsapp_channel
from translations import t

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0

# Matches exactly the two tags translations.py's alert templates use:
# <a href="URL">text</a> and <b>text</b>. Anything else is stripped as plain
# text — this is a small, deliberately narrow converter for the specific
# markup this codebase produces, not a general HTML-to-Markdown library.
_LINK_RE = re.compile(r'<a\s+href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
_BOLD_RE = re.compile(r"<b>(.*?)</b>", re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_whatsapp_text(html_text: str) -> str:
    """
    Convert the small subset of Telegram-HTML used by translations.t()'s alert
    templates into WhatsApp's markdown-lite formatting.
      <a href="URL">text</a> -> "text: URL"  (WhatsApp has no markdown link
        syntax; it auto-linkifies bare URLs in plain text, so the link is
        simply included as a trailing URL rather than dropped)
      <b>text</b>            -> "*text*"     (WhatsApp bold)
      any other tag          -> stripped
    HTML entities (&amp; etc.) are unescaped last so a literal "&" inside a
    product name isn't left as "&amp;".
    """
    text = _LINK_RE.sub(r"\2: \1", html_text)
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _ANY_TAG_RE.sub("", text)
    return html_module.unescape(text)


async def forward_alert(
    product: dict, *, name: str, site: str, alert_url: str, price: float | None, lang: str
) -> None:
    """
    Best-effort forward of one "back in stock" alert to the product owner's
    OWN registered WhatsApp channel (per-user, admin-approved — see
    database.whatsapp_channels). No-ops silently (with a log line) if the
    feature isn't configured, the user has no active channel, or the forward
    request fails for any reason.
    """
    try:
        if not WHATSAPP_FORWARDER_URL:
            return  # feature not configured at all — fully inert

        invite_link = get_active_whatsapp_channel(product["user_id"])
        if not invite_link:
            return  # no active registration for this user

        price_line = ""
        if price is not None:
            price_line = t("stock_alert_price_line", lang, price=f"{price:,.0f}")
        html_text = t(
            "stock_alert", lang,
            name=name, site=site.capitalize(), price_line=price_line, url=alert_url,
        )
        wa_text = _html_to_whatsapp_text(html_text)

        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{WHATSAPP_FORWARDER_URL}/forward",
                headers={"Authorization": f"Bearer {WHATSAPP_FORWARDER_SECRET}"},
                json={"invite_link": invite_link, "text": wa_text},
            )
        if resp.status_code != 200:
            logger.error(
                f"[whatsapp] forwarder returned HTTP {resp.status_code} for "
                f"user {product['user_id']}: {resp.text[:200]}"
            )
        else:
            logger.info(
                f"[whatsapp] queued alert for user {product['user_id']} "
                f"product #{product['id']}"
            )
    except Exception as exc:
        logger.error(f"[whatsapp] forward failed (non-fatal): {exc}")
