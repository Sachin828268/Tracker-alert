import json
import logging
import re

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Documentation-only (not read by any code — see stock_checker._JS_SITES
# for the actual render=true/false switch, and
# stock_checker._EXTRA_RETRY_ON_INCOMPLETE_SITES for the "don't guess a
# verdict from a still-blocked page" retry/skip logic).
NEEDS_JS = True

# WooCommerce's standard availability_html markup for a product/variation
# is a <p class="stock in-stock">In stock</p> or
# <p class="stock out-of-stock">Out of stock</p> element. This site
# renders variations into a JSON blob (e.g. a variation-form data
# attribute or an embedded <script> state object) where that HTML is
# embedded as a JSON STRING value, so its quotes come out backslash-
# escaped in the raw response (class=\"stock out-of-stock\"). The
# backslash is optional in this pattern so it matches both that escaped
# form and a plain, directly-rendered one.
_STOCK_CLASS_PATTERN = re.compile(r'class=\\?"stock (in-stock|out-of-stock)\\?"', re.IGNORECASE)


def _count_woocommerce_stock_classes(html: str) -> tuple[int, int]:
    """Count every occurrence of WooCommerce's stock-status class pattern
    in the raw HTML — one occurrence per product variation on a
    variable-product page. Returns (in_stock_count, out_of_stock_count)."""
    in_stock_count = 0
    out_of_stock_count = 0
    for match in _STOCK_CLASS_PATTERN.finditer(html):
        if match.group(1).lower() == "in-stock":
            in_stock_count += 1
        else:
            out_of_stock_count += 1
    return in_stock_count, out_of_stock_count


def _offer_availability(offers) -> str:
    """Extract the first availability string from a JSON-LD 'offers'
    value that may be a single Offer dict, an AggregateOffer dict
    wrapping a nested offers list, or a plain list of Offer dicts."""
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


def check(soup: BeautifulSoup, html: str) -> bool:
    """
    inventstore.in's stock-detection logic, now based on the confirmed
    WooCommerce variation stock-class pattern rather than page text.

    The previous "Buy Now"/"Add to Cart" text-based primary signal has
    been removed entirely — confirmed unreliable, since that button text
    is static on this site's product pages regardless of actual stock
    status. The generic embedded-JSON substring key check (a prior
    fallback here) is also removed: on a page with multiple product
    variations, a plain "first occurrence wins" substring match can't
    tell "this ONE variation is unavailable" apart from "the WHOLE
    product is unavailable" — exactly the ambiguity this WooCommerce-
    specific, occurrence-counting approach is built to resolve correctly
    instead.

    Detection order:
    1. JSON-LD product-level availability (kept — a structured,
       whole-product signal, not per-variation free text, and not
       implicated in the issues that led to this change).
    2. WooCommerce variation stock classes (see
       _count_woocommerce_stock_classes): if ANY occurrence is
       class="stock in-stock", the product is in stock — at least one
       purchasable variation exists. Only if EVERY matched occurrence is
       class="stock out-of-stock" (and at least one was found) is the
       product reported out of stock.
    3. No signal at all -> defaults to out of stock, per this codebase's
       standing principle that a missed alert is safer than a false one.
    """
    # ── JSON-LD ──────────────────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            avail = _offer_availability(item.get("offers", {}))
            if "InStock" in avail:
                logger.info("[inventstore] JSON-LD: InStock → True")
                return True
            if "OutOfStock" in avail or "Discontinued" in avail:
                logger.info("[inventstore] JSON-LD: OutOfStock/Discontinued → False")
                return False

    # ── WooCommerce variation stock classes ─────────────────────────────
    in_stock_count, out_of_stock_count = _count_woocommerce_stock_classes(html)
    if in_stock_count > 0:
        logger.info(
            f"[inventstore] WooCommerce variation classes: {in_stock_count} "
            f"in-stock, {out_of_stock_count} out-of-stock → True (at least "
            f"one purchasable variation)"
        )
        return True
    if out_of_stock_count > 0:
        logger.info(
            f"[inventstore] WooCommerce variation classes: {out_of_stock_count} "
            f"out-of-stock, 0 in-stock → False"
        )
        return False

    logger.info("[inventstore] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
