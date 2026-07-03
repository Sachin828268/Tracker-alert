import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to bag", "buy now", "notify me when back"]
_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon", "notify me",
]

# OnePlus-specific cart/buy CSS classes (best-effort — not yet confirmed
# against real production HTML; logged via _log_diagnostics so any wrong
# assumption here shows up in Railway logs instead of guessed at again).
_CART_CLASSES = ["add-to-cart", "addToCart", "buy-now", "buyNow", "add-to-bag"]

# Class tokens that mark a button/anchor as DISABLED. Croma lesson applied:
# match the substring "disable" (not the full word "disabled") so class names
# like "disableBuyNow" or "btn-disable" are caught too, not just the exact
# word "disabled" — but see checkers/croma.py's own history for why a bare
# "disable" substring can ALSO false-positive on a structural hook class that
# happens to contain it (e.g. "disable-btn-in-pdp" present on active buttons
# too). No such structural class is known for OnePlus yet, so the substring
# is used as-is here; if production logs ever show an active button being
# misflagged, add the offending class to an explicit exclusion list rather
# than removing the substring check.
_DISABLED_CLASS_MARKERS = ("disable", "inactive")


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled."""
    if el.get("disabled") is not None:
        return True
    if el.get("aria-disabled", "").lower() == "true":
        return True
    classes = " ".join(el.get("class", [])).lower()
    return any(marker in classes for marker in _DISABLED_CLASS_MARKERS)


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


def _log_diagnostics(soup: BeautifulSoup, html: str) -> None:
    """
    Dump the decision trail to logs: JSON-LD availability, embedded-JSON stock
    keys, OOS text matches, and cart/buy button state (class/attrs/computed
    disabled). Matches the Croma/BigBasket/Blinkit diagnostic pattern so any
    wrong assumption in this brand-new checker is visible and correctable from
    real Railway logs rather than guessed at again. Log-only: never changes
    the returned value.
    """
    html_lower = html.lower()
    logger.info(f"[oneplus][diag] HTML length={len(html)}, head={html[:200]!r}")

    found_ld = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("offers") is not None:
                avail = _offer_availability(item.get("offers", {}))
                if avail:
                    found_ld = True
                    logger.info(f"[oneplus][diag] JSON-LD availability={avail!r}")
    if not found_ld:
        logger.info("[oneplus][diag] JSON-LD availability: none found")

    for key in (
        '"in_stock":true', '"inStock":true', '"is_available":true', '"isAvailable":true',
        '"in_stock":false', '"inStock":false', '"is_available":false', '"isAvailable":false',
    ):
        if key in html:
            logger.info(f"[oneplus][diag] embedded JSON key present: {key!r}")

    oos_hits = [p for p in _OOS_PATTERNS if p in html_lower]
    logger.info(f"[oneplus][diag] OOS text patterns present: {oos_hits or 'none'}")

    btn_count = 0
    for el in soup.find_all(["button", "a"]):
        label = " ".join(filter(None, [
            el.get_text(" ", strip=True),
            el.get("aria-label", "") or "",
            el.get("data-testid", "") or "",
            el.get("id", "") or "",
            " ".join(el.get("class", []) or []),
        ])).lower()
        if any(pat in label for pat in _ADD_PATTERNS):
            logger.info(
                f"[oneplus][diag] cart/buy <{el.name}> "
                f"text={el.get_text(' ', strip=True)[:40]!r} class={el.get('class')} "
                f"disabled_attr={el.get('disabled')!r} aria-disabled={el.get('aria-disabled')!r} "
                f"→ _is_disabled={_is_disabled(el)}"
            )
            btn_count += 1
            if btn_count >= 10:
                break
    if btn_count == 0:
        logger.info("[oneplus][diag] no cart/buy button/anchor matched")


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    _log_diagnostics(soup, html)

    # ── JSON-LD (most reliable when present) ────────────────────────────────
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
                    logger.info("[oneplus] JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[oneplus] JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── Embedded JSON — OnePlus's own stock field (Next.js/React apps often
    #    inline product state as JSON in a <script> tag) ────────────────────
    for key in ('"in_stock":true', '"inStock":true', '"is_available":true', '"isAvailable":true'):
        if key in html:
            logger.info(f"[oneplus] embedded JSON key {key!r} → True")
            return True
    for key in ('"in_stock":false', '"inStock":false', '"is_available":false', '"isAvailable":false'):
        if key in html:
            logger.info(f"[oneplus] embedded JSON key {key!r} → False")
            return False

    # ── Explicit OOS text (checked before buttons — a disabled/greyed button's
    #    surrounding text often carries the clearest signal on record pages) ──
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[oneplus] OOS text: '{pattern}' → False")
            return False

    # ── Buy/cart button & anchor scan — skip disabled ───────────────────────
    for cls in _CART_CLASSES:
        for el in soup.find_all(class_=cls):
            if _is_disabled(el):
                logger.info(f"[oneplus] class '{cls}' on <{el.name}> is disabled — skipping")
                continue
            logger.info(f"[oneplus] active class '{cls}' on <{el.name}> → True")
            return True

    for el in soup.find_all(["button", "a"]):
        if _is_disabled(el):
            continue
        text = el.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[oneplus] active <{el.name}> '{text[:40]}' → True")
            return True

    # ── Attribute checks ─────────────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            # Attribute values are typically hyphenated/camelCase (e.g.
            # "add-to-cart-btn"), not natural-language phrases with spaces —
            # _ADD_PATTERNS alone (meant for button TEXT) won't match those,
            # so hyphen/no-space variants are checked explicitly too (same
            # pattern used by checkers/bigbasket.py, blinkit.py, instamart.py).
            if (
                "add-to-cart" in val or "addtocart" in val
                or "add-to-bag" in val or "addtobag" in val
                or "buy-now" in val or "buynow" in val
                or any(p in val for p in _ADD_PATTERNS)
            ):
                logger.info(f"[oneplus] active {attr}='{val[:40]}' → True")
                return True

    # ── Price presence fallback — only reached if nothing above fired and no
    #    OOS text was found; a listed ₹ price with no negative signal is a
    #    weak but reasonable positive (same pattern used by bigbasket.py) ────
    if "₹" in html:
        logger.info("[oneplus] ₹ price symbol present, no OOS signals → True")
        return True

    logger.info("[oneplus] no conclusive signal → defaulting OUT OF STOCK (False)")
    return False
