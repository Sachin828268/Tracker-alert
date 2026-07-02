import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "buy now", "add to bag"]

# Delivery-section strings confirmed on real OOS Croma pages (e.g. vivo T5X).
# Checked FIRST — before JSON-LD (which can be stale) and before button text
# (which appears on both in-stock and OOS pages) — so they act as overrides.
_DELIVERY_RESTRICTION_PATTERNS = [
    "not available for your pincode",
    "unfortunately not available for your location",
]

_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]
# "btn-cart" removed — it matched Croma's persistent header cart icon on every page,
# causing false positives once the lambda was using correct BS4 class membership.
# "addToCart" kept — specific enough as an exact class name.
_CART_CLASSES = ["add-to-cart", "addToCart", "plp-add-to-cart"]


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled."""
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return "disabled" in classes or "inactive" in classes


def _offer_availability(offers) -> str:
    """
    Extract the first availability string from an 'offers' value that may be:
      • a single Offer dict     {"availability": "https://schema.org/InStock"}
      • an AggregateOffer dict  {"offers": [{"availability": "..."}], ...}
      • a list of Offer dicts   [{"availability": "..."}, ...]
    Returns "" when no availability can be found.
    """
    if isinstance(offers, dict):
        avail = offers.get("availability", "")
        if avail:
            return str(avail)
        # AggregateOffer: availability lives in the nested offers list
        nested = offers.get("offers", [])
        if isinstance(nested, list):
            for o in nested:
                if isinstance(o, dict):
                    a = o.get("availability", "")
                    if a:
                        return str(a)
        elif isinstance(nested, dict):
            a = nested.get("availability", "")
            if a:
                return str(a)
    elif isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict):
                a = o.get("availability", "")
                if a:
                    return str(a)
    return ""


def _log_delivery_diagnostics(soup: BeautifulSoup, html: str) -> None:
    """
    Dump the REAL scraped delivery-section structure to the logs.

    The previous fix assumed "Not Available for your pincode" appears as literal
    text in the scraped HTML. That assumption passed synthetic tests but fails on
    the live page, so we log exactly what the real HTML contains — pattern hits,
    delivery-related elements (whatever their actual class names are), and context
    around key phrases — so the true structure can be read straight from prod logs
    instead of guessed at. Log-only: this function never changes the result.
    """
    html_lower = html.lower()
    logger.info(f"[croma][diag] HTML length={len(html)}")
    # Confirm we got a real product page, not a bot-challenge / block page.
    logger.info(f"[croma][diag] head: {html[:200]!r}")

    # 1. Exact per-pattern substring presence (what the checker relies on).
    for p in _DELIVERY_RESTRICTION_PATTERNS:
        logger.info(f"[croma][diag] restriction pattern {p!r} in html: {p in html_lower}")

    # 2. Broader keyword presence — reveals alternate phrasing / whether the
    #    serviceability text is present in the scraped HTML at all.
    for kw in (
        "not available", "not serviceable", "unfortunately", "pincode",
        "pin code", "deliver by", "delivered by", "delivery at", "check delivery",
        "enter pincode", "enter your pincode", "notify me", "sold out",
    ):
        if kw in html_lower:
            logger.info(f"[croma][diag] keyword present: {kw!r}")

    # 3. Every element whose class hints at delivery/serviceability, with its
    #    ACTUAL class list and text — so we learn the real class names.
    hits = 0
    for el in soup.find_all(class_=True):
        cls = " ".join(el.get("class", [])).lower()
        if any(tok in cls for tok in ("deliver", "pincode", "serviceab", "availab", "location")):
            txt = el.get_text(" ", strip=True)[:120]
            logger.info(f"[croma][diag] el <{el.name}> class={el.get('class')} text={txt!r}")
            hits += 1
            if hits >= 25:  # cap noise
                logger.info("[croma][diag] (delivery-ish element dump capped at 25)")
                break

    # 4. Context excerpts around the phrases we care about most.
    for kw in ("not available", "unfortunately", "pincode", "notify me"):
        idx = html_lower.find(kw)
        if idx != -1:
            logger.info(f"[croma][diag] ...{html[max(0, idx - 90):idx + 90]!r}...")


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    _log_delivery_diagnostics(soup, html)

    # ── Delivery restriction — highest priority, overrides all other signals ───
    # On OOS pages Croma's delivery section shows "Not Available for your pincode"
    # or "Unfortunately not available for your location" (confirmed on real pages).
    # These are checked before JSON-LD (which may carry stale InStock data) and
    # before button text (which exists on both in-stock and OOS pages).
    for pattern in _DELIVERY_RESTRICTION_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] delivery restriction: '{pattern}' → False")
            return False

    # Also inspect the delivery-text-msg element directly for broader coverage
    # (catches phrasing variants not in the pattern list above).
    delivery_el = soup.find(class_="delivery-text-msg")
    if delivery_el:
        dtxt = delivery_el.get_text(" ", strip=True).lower()
        logger.info(f"[croma] delivery-text-msg: '{dtxt[:80]}'")
        if "not available" in dtxt or "unfortunately" in dtxt:
            logger.info("[croma] delivery-text-msg unavailable text → False")
            return False

    # ── JSON-LD pass — OutOfStock trusted immediately; InStock deferred ────────
    # Croma's structured data has been observed returning InStock for products
    # that are actually out of stock (stale / incorrect data). Trusting it
    # immediately caused every product to appear in-stock.
    # Strategy: return False on OutOfStock right away (reliable negative signal),
    # but hold any InStock signal and only confirm it after OOS text patterns
    # have had a chance to contradict it.
    json_ld_in_stock = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = _offer_availability(item.get("offers", {}))
                if not avail:
                    continue
                if "InStock" in avail:
                    logger.info("[croma] JSON-LD: InStock (deferred — checking OOS text first)")
                    json_ld_in_stock = True
                elif "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[croma] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── OOS text patterns ─────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] OOS text: '{pattern}' → False")
            return False

    # ── JSON-LD InStock confirmed (OOS text did not contradict it) ────────────
    if json_ld_in_stock:
        logger.info("[croma] JSON-LD InStock confirmed (no OOS text) → True")
        return True

    # ── Cart button classes (exact class membership via BS4 class_= filter) ───
    # NOTE: Previously used attrs={"class": lambda c: cls in " ".join(c)} which
    # is BROKEN — BS4 passes individual class strings to the lambda, so
    # " ".join(str) character-joins rather than word-joins. Use class_=cls
    # instead, which BS4 correctly resolves to exact class-membership testing.
    for cls in _CART_CLASSES:
        for el in soup.find_all(class_=cls):
            if _is_disabled(el):
                logger.info(f"[croma] class '{cls}' on <{el.name}> is disabled — skipping")
                continue
            logger.info(f"[croma] active class '{cls}' on <{el.name}> → True")
            return True

    # ── Buttons — skip disabled ────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        if _is_disabled(btn):
            continue
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[croma] active button '{text[:40]}' → True")
            return True

    # ── Attribute checks ──────────────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[croma] active {attr}='{val[:40]}' → True")
                return True

    logger.info("[croma] no conclusive signal → False")
    return False
