"""
playwright_scraper/main.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Standalone pilot scraper service (Playwright + Chromium) for iQOO and Vivo,
built to test replacing their current Scrape.do render=true checks — those
burn render credits at scale; this self-hosts the same JS-rendering step
behind an optional metered proxy instead.

ISOLATION: this is a completely separate Railway service — its own
container, its own process, its own requirements.txt (Playwright is NOT a
dependency of the main bot). The main bot's existing iQOO/Vivo Scrape.do
checkers (checkers/iqoo.py, checkers/vivo.py) are UNTOUCHED and remain live
in production; nothing here is wired into the main bot yet. If this crashes,
gets blocked, or a proxy runs out of quota, the main bot is unaffected — it
simply isn't calling this service (nothing does, yet).

HTTP surface:
  POST /check-stock  Body: {"url": str, "store": "iqoo"|"vivo"}. Returns
                      {"url", "store", "in_stock": bool|None, "signal": str,
                      "attempts": int}. in_stock is None ("check failed")
                      when no conclusive signal was found after retrying —
                      NEVER a guessed False, unlike the main bot's own
                      checkers (deliberate: this is a pilot being tuned, an
                      inconclusive read should surface for investigation,
                      not silently default to "out of stock").
  GET  /health        Unauthenticated: {"ok", "max_concurrent_checks",
                      "proxy_configured", "supported_stores"}.

Stock-detection logic for iqoo/vivo (check_iqoo_vivo_stock, _OOS_PATTERNS,
_offer_availability) is PORTED from checkers/iqoo.py and checkers/vivo.py —
both already probe-confirmed reliable (JSON-LD offers.availability primary,
embedded-JSON fallback, explicit OOS text last resort) against real
in-stock/OOS product pages. This sandbox has no live network access to
verify the signal still holds when sourced via Playwright+proxy instead of
Scrape.do's render=true — see README.md for the live-verification steps
this needs once deployed.
"""

import json
import logging
import os
import threading
import time

from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("playwright_scraper")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "8080"))
HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"

# Requirement #7 ("Add resource limits: cap con...") — the message cut off
# there; taken as "cap concurrent browser instances", since that's the
# standard resource-exhaustion risk for a self-hosted scraper (each headless
# Chromium instance can use 150-300MB+ RAM). A Semaphore bounds how many
# checks run in parallel; excess requests queue for a free slot rather than
# spawning unbounded browsers. Flag if a different limit was meant (memory
# ceiling, requests/min, etc.) — Railway's own container limits are separate
# and unaffected by this.
MAX_CONCURRENT_CHECKS = int(os.getenv("MAX_CONCURRENT_CHECKS", "2"))
# How long an incoming request waits for a free concurrency slot before
# giving up (returns a "check failed" result rather than queueing forever).
SLOT_WAIT_TIMEOUT_SECONDS = float(os.getenv("SLOT_WAIT_TIMEOUT_SECONDS", "60"))

NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "20000"))
# How long to wait specifically for the primary stock signal (a JSON-LD
# script tag) to appear before giving up on THIS attempt — requirement #6's
# "stock element isn't found within a reasonable timeout". Not a hard
# failure: the fallback signals (embedded JSON, OOS text) still run against
# whatever HTML is present even if this wait times out.
SIGNAL_WAIT_TIMEOUT_MS = int(os.getenv("SIGNAL_WAIT_TIMEOUT_MS", "8000"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS = float(os.getenv("RETRY_DELAY_SECONDS", "2"))

# Webshare (or any HTTP-auth) proxy — entirely optional. Unset PROXY_HOST
# means "no proxy", so this runs directly for local testing before buying a
# proxy plan; set all four once you have Webshare credentials.
PROXY_HOST = os.getenv("PROXY_HOST", "")
PROXY_PORT = os.getenv("PROXY_PORT", "")
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "")


def _proxy_config() -> dict | None:
    """Playwright's launch(proxy=...) dict, or None if PROXY_HOST/PORT
    aren't both set — proxy is fully optional, never required to run."""
    if not PROXY_HOST or not PROXY_PORT:
        return None
    cfg = {"server": f"http://{PROXY_HOST}:{PROXY_PORT}"}
    if PROXY_USERNAME:
        cfg["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        cfg["password"] = PROXY_PASSWORD
    return cfg


# ---------------------------------------------------------------------------
# Bandwidth optimization: block everything except what's needed to read the
# DOM/JS-injected stock signal. This is the whole point of self-hosting
# against a metered (GB-priced) proxy — a product page with full images can
# be 2-5MB; blocking image/font/media/stylesheet cuts that dramatically
# since we only ever read page.content(), never render anything visually.
# ---------------------------------------------------------------------------
_ALLOWED_RESOURCE_TYPES = {"document", "script", "xhr", "fetch"}


def _make_resource_blocker(stats: dict):
    """Returns a Playwright route handler bound to a per-request stats dict
    (allowed/blocked counts + bytes-so-far isn't available pre-response, so
    we count requests, not bytes) — logged after each check so bandwidth
    savings are visible, not just assumed."""

    def _handler(route):
        resource_type = route.request.resource_type
        if resource_type in _ALLOWED_RESOURCE_TYPES:
            stats["allowed"] = stats.get("allowed", 0) + 1
            route.continue_()
        else:
            stats["blocked"] = stats.get("blocked", 0) + 1
            route.abort()

    return _handler


# ---------------------------------------------------------------------------
# Stock detection — ported from checkers/iqoo.py and checkers/vivo.py
# (identical logic in both; both stores' probe found JSON-LD availability
# reliably differentiates OOS vs in-stock). Returns (in_stock, signal):
# in_stock is True/False for a confident read, None when nothing conclusive
# was found THIS attempt (the caller retries on None rather than defaulting
# to False — see MAX_RETRIES / _fetch_and_check).
# ---------------------------------------------------------------------------
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me", "coming soon", "temporarily unavailable",
]


def _offer_availability(offers) -> str:
    """Extract the first availability string from an 'offers' value that may
    be a single Offer dict, an AggregateOffer dict wrapping a nested offers
    list, or a plain list of Offer dicts. Returns "" when none is found."""
    if isinstance(offers, dict):
        avail = offers.get("availability", "")
        if avail:
            return str(avail)
        nested = offers.get("offers", [])
        if isinstance(nested, list):
            for o in nested:
                if isinstance(o, dict) and o.get("availability"):
                    return str(o["availability"])
        elif isinstance(nested, dict) and nested.get("availability"):
            return str(nested["availability"])
    elif isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict) and o.get("availability"):
                return str(o["availability"])
    return ""


def check_iqoo_vivo_stock(soup: BeautifulSoup, html: str) -> tuple[bool | None, str]:
    html_lower = html.lower()

    # ── JSON-LD (primary, proven-reliable signal per the original probe) ──
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            avail = _offer_availability(item.get("offers", {}))
            if not avail:
                continue
            if "InStock" in avail:
                return True, f"JSON-LD offers.availability={avail!r}"
            if "OutOfStock" in avail or "Discontinued" in avail:
                return False, f"JSON-LD offers.availability={avail!r}"

    # ── Embedded JSON (fallback if JSON-LD is ever absent) ─────────────────
    for key in ('"in_stock":true', '"inStock":true', '"is_available":true', '"isAvailable":true'):
        if key in html:
            return True, f"embedded JSON key {key!r} present"
    for key in ('"in_stock":false', '"inStock":false', '"is_available":false', '"isAvailable":false'):
        if key in html:
            return False, f"embedded JSON key {key!r} present"

    # ── Explicit OOS text (last-resort negative signal) ─────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            return False, f"OOS text pattern {pattern!r} found"

    return None, "no conclusive signal found on this attempt"


CHECKERS = {
    "iqoo": check_iqoo_vivo_stock,
    "vivo": check_iqoo_vivo_stock,
}


# ---------------------------------------------------------------------------
# Rendering + retry
# ---------------------------------------------------------------------------
_check_semaphore = threading.Semaphore(MAX_CONCURRENT_CHECKS)


def _render_page(url: str) -> str:
    """Launch a fresh, isolated browser for this one check (simplest
    possible isolation between requests — no shared state, no thread-safety
    concerns with Playwright's sync API — at the cost of ~1-2s browser
    startup per check, acceptable for a pilot's request volume). Bounded by
    _check_semaphore so at most MAX_CONCURRENT_CHECKS browsers run at once."""
    acquired = _check_semaphore.acquire(timeout=SLOT_WAIT_TIMEOUT_SECONDS)
    if not acquired:
        raise RuntimeError(
            f"too many concurrent checks (max {MAX_CONCURRENT_CHECKS}) — "
            f"timed out after {SLOT_WAIT_TIMEOUT_SECONDS}s waiting for a free slot"
        )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=HEADLESS, proxy=_proxy_config())
            try:
                context = browser.new_context()
                page = context.new_page()
                stats: dict = {}
                page.route("**/*", _make_resource_blocker(stats))
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                try:
                    page.wait_for_selector(
                        'script[type="application/ld+json"]', timeout=SIGNAL_WAIT_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError:
                    logger.info(
                        f"[render] JSON-LD script tag didn't appear within "
                        f"{SIGNAL_WAIT_TIMEOUT_MS}ms for {url} — proceeding with "
                        f"whatever rendered (fallback signals may still catch it)"
                    )
                html = page.content()
                logger.info(
                    f"[render] {url}: {stats.get('allowed', 0)} requests allowed, "
                    f"{stats.get('blocked', 0)} blocked (image/font/media/stylesheet)"
                )
                return html
            finally:
                browser.close()
    finally:
        _check_semaphore.release()


def _fetch_and_check(url: str, store: str) -> dict:
    checker = CHECKERS[store]
    last_signal = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            html = _render_page(url)
        except Exception as exc:
            last_signal = f"page render failed: {exc}"
            logger.warning(f"[check-stock] attempt {attempt}/{MAX_RETRIES} render failed for {url}: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
            continue

        soup = BeautifulSoup(html, "html.parser")
        in_stock, signal = checker(soup, html)
        if in_stock is not None:
            logger.info(f"[check-stock] {url} -> in_stock={in_stock} ({signal}) on attempt {attempt}")
            return {"in_stock": in_stock, "signal": signal, "attempts": attempt}

        last_signal = signal
        logger.info(f"[check-stock] attempt {attempt}/{MAX_RETRIES}: {signal} for {url} — retrying")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error(f"[check-stock] check failed after {MAX_RETRIES} attempts for {url}: {last_signal}")
    return {
        "in_stock": None,
        "signal": f"check failed after {MAX_RETRIES} attempts — last result: {last_signal}",
        "attempts": MAX_RETRIES,
    }


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/check-stock", methods=["POST"])
    def check_stock():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        store = (data.get("store") or "").strip().lower()
        if not url or not store:
            return jsonify({"error": "url and store are required"}), 400
        if store not in CHECKERS:
            return jsonify({
                "error": f"unsupported store {store!r}",
                "supported_stores": sorted(CHECKERS),
            }), 400

        result = _fetch_and_check(url, store)
        return jsonify({"url": url, "store": store, **result}), 200

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "ok": True,
            "max_concurrent_checks": MAX_CONCURRENT_CHECKS,
            "proxy_configured": _proxy_config() is not None,
            "supported_stores": sorted(CHECKERS),
        })

    return app


def main() -> None:
    app = create_app()
    from waitress import serve
    logger.info(f"[http] serving on 0.0.0.0:{PORT} (max_concurrent_checks={MAX_CONCURRENT_CHECKS})")
    serve(app, host="0.0.0.0", port=PORT, threads=MAX_CONCURRENT_CHECKS + 2)


if __name__ == "__main__":
    main()
