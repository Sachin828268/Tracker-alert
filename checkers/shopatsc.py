import logging
import time

import httpx
from bs4 import BeautifulSoup

from .common import build_scraper_url, HEADERS

logger = logging.getLogger(__name__)

# This site does NOT participate in stock_checker._JS_SITES — it owns its
# own two-stage render escalation (render=false first, render=true only if
# needed; see check_via_html below), special-cased in
# stock_checker.check_stock() for "shopatsc", the opposite order from
# every render=true-by-default site in _JS_SITES.

# Scrape.do fetch timeout for either render mode.
_RENDER_TIMEOUT = 30.0

_ADD_PATTERNS = ["add to cart", "buy now"]
_NOTIFY_ONLY_PATTERN = "notify me"

# Minimum visible-text length considered "plausibly a real, fully-loaded
# product page". A render=false fetch that comes back shorter than this
# (or failed outright) is treated as incomplete and retried with
# render=true.
_MIN_PLAUSIBLE_TEXT_LENGTH = 200


def _visible_text(html: str) -> str:
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    return text_soup.get_text(" ", strip=True)


def _text_looks_incomplete(visible_text: str) -> bool:
    return len(visible_text) < _MIN_PLAUSIBLE_TEXT_LENGTH


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    Sole stock-detection signal for ShopAtSC (Sony India's official PS5
    store). The Shopify '.js' JSON product endpoint's "available" field
    was confirmed unreliable for this store specifically — both a real
    in-stock and a real out-of-stock product returned available: true,
    most likely because ShopAtSC runs a separate "Notify Me" waitlist app
    that doesn't touch Shopify's native inventory tracking (which is what
    the .js endpoint actually reflects). Reliance on that endpoint has
    been removed entirely; detection is HTML-text-only: an active "Add to
    cart"/"Buy Now" affordance in the visible text means in stock; a lone
    "Notify Me" affordance with no "Add to cart"/"Buy Now" present means
    out of stock. Defaults to out of stock when neither is found.
    """
    visible_text = _visible_text(html).lower()

    if any(p in visible_text for p in _ADD_PATTERNS):
        logger.info("[shopatsc] add-to-cart/buy-now text found → True (in stock)")
        return True

    if _NOTIFY_ONLY_PATTERN in visible_text:
        logger.info("[shopatsc] 'notify me' found, no add-to-cart → False (out of stock)")
        return False

    logger.info("[shopatsc] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False


async def _fetch_page(url: str, render_js: bool) -> httpx.Response:
    scraper_url = build_scraper_url(url, render_js=render_js)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=_RENDER_TIMEOUT) as client:
        return await client.get(scraper_url)


async def check_via_html(url: str) -> bool:
    """
    Fetches the product page via Scrape.do and returns the stock status
    via check(). Tries render=false FIRST — Shopify product pages are
    largely server-rendered, so the "Add to cart"/"Notify Me" text this
    checker needs is usually present without executing JS, and render=false
    is both faster and cheaper in Scrape.do credits than render=true. If
    that fetch fails (non-200) or its visible-text extraction looks
    incomplete/empty, retries once with render=true.

    Called directly by stock_checker.check_stock() (special-cased for
    "shopatsc", same pattern used for Apple's pincode refinement) rather
    than going through the generic per-site _JS_SITES flag, since the
    render=false-first-then-escalate order is the opposite of every other
    JS-rendered site in this codebase.
    """
    resp = await _fetch_page(url, render_js=False)
    text = _visible_text(resp.text) if resp.status_code == 200 else ""

    if resp.status_code != 200 or _text_looks_incomplete(text):
        logger.info(
            f"[shopatsc] render=false insufficient (status={resp.status_code}, "
            f"text_len={len(text)}) — retrying render=true"
        )
        resp = await _fetch_page(url, render_js=True)
        resp.raise_for_status()

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    return check(soup, html)


async def debug_check(url: str) -> dict:
    """
    Diagnostic version of check_via_html()'s two-stage render escalation
    (render=false first, render=true only if needed) for the
    /debugsonyofficial admin command (admin_handlers.py) — NOT used by the
    live check_stock() path (which calls check_via_html() directly and
    only cares about the final bool). Runs through the exact same logic
    but captures which render mode was used, the HTTP status/visible-text
    length/timing for EACH stage, the final signal, and total elapsed
    time — instead of collapsing straight to a bool — so slowness or an
    unexpected render mode can be diagnosed from a single command without
    touching production code.
    """
    start = time.monotonic()
    result: dict = {
        "url": url,
        "render_false_status_code": None,
        "render_false_error": None,
        "render_false_visible_text_length": None,
        "render_false_looked_incomplete": None,
        "render_false_elapsed_seconds": None,
        "used_render_true_fallback": False,
        "render_true_status_code": None,
        "render_true_error": None,
        "render_true_visible_text_length": None,
        "render_true_elapsed_seconds": None,
        "signal": None,
        "in_stock": None,
        "total_elapsed_seconds": None,
    }

    stage1_start = time.monotonic()
    html1 = None
    try:
        resp1 = await _fetch_page(url, render_js=False)
        result["render_false_status_code"] = resp1.status_code
        if resp1.status_code == 200:
            html1 = resp1.text
        else:
            result["render_false_error"] = f"HTTP {resp1.status_code}"
    except Exception as exc:
        result["render_false_error"] = f"{type(exc).__name__}: {exc}"
    result["render_false_elapsed_seconds"] = time.monotonic() - stage1_start

    text1 = _visible_text(html1) if html1 is not None else ""
    result["render_false_visible_text_length"] = len(text1)
    incomplete = html1 is None or _text_looks_incomplete(text1)
    result["render_false_looked_incomplete"] = incomplete

    final_html = html1
    if incomplete:
        result["used_render_true_fallback"] = True
        stage2_start = time.monotonic()
        html2 = None
        try:
            resp2 = await _fetch_page(url, render_js=True)
            result["render_true_status_code"] = resp2.status_code
            if resp2.status_code == 200:
                html2 = resp2.text
            else:
                result["render_true_error"] = f"HTTP {resp2.status_code}"
        except Exception as exc:
            result["render_true_error"] = f"{type(exc).__name__}: {exc}"
        result["render_true_elapsed_seconds"] = time.monotonic() - stage2_start
        if html2 is not None:
            result["render_true_visible_text_length"] = len(_visible_text(html2))
        final_html = html2

    if final_html is None:
        result["signal"] = "no usable HTML from either render=false or render=true"
        result["total_elapsed_seconds"] = time.monotonic() - start
        return result

    text_to_check = _visible_text(final_html).lower()
    matched_add = next((p for p in _ADD_PATTERNS if p in text_to_check), None)
    if matched_add:
        result["in_stock"] = True
        result["signal"] = f"matched add-pattern {matched_add!r}"
    elif _NOTIFY_ONLY_PATTERN in text_to_check:
        result["in_stock"] = False
        result["signal"] = f"matched {_NOTIFY_ONLY_PATTERN!r}, no add-to-cart pattern found"
    else:
        result["in_stock"] = False
        result["signal"] = "no add-pattern or 'notify me' text found — defaulted to False"

    result["total_elapsed_seconds"] = time.monotonic() - start
    return result
