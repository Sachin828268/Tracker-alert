import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

# Primary signals — explicit phrases requested for TataNeu specifically.
# Not yet verified against real captured HTML (no HAR available for this
# site); if production [diag] logs show these never firing while the
# JSON-LD/button fallback below does the real work, that's expected and fine —
# it just means these aren't the phrases TataNeu actually uses.
_OOS_TEXT = "the product is not available"
_IN_STOCK_TEXTS = ("get it now!", "delivery options for")

# Generic OOS/add patterns for the fallback stage, matching the rest of the
# checkers/ codebase's convention.
_ADD_PATTERNS = ["add to cart", "add to bag", "buy now"]
_OOS_PATTERNS = ["out of stock", "sold out", "currently unavailable", "notify me when available"]

# Class tokens that mark a button/anchor as DISABLED via CSS alone, with no
# `disabled`/`aria-disabled` HTML attribute present — the pattern discovered
# on Croma (see checkers/croma.py's history). Applied here pre-emptively
# since every quick-commerce-style checker in this codebase has needed it.
_DISABLED_CLASS_MARKERS = ("disable", "inactive")


def _is_disabled(el) -> bool:
    """Return True if a BS4 element is visually/semantically disabled — via
    the `disabled` attribute, `aria-disabled="true"`, or a _DISABLED_CLASS_MARKERS
    substring in its class list."""
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
    Dump the decision trail to logs: the two explicit text-pattern hits,
    JSON-LD availability, generic OOS text matches, and every candidate
    cart/buy button's text/class/computed-disabled state. Log-only: never
    changes the returned value. First-run evidence for this brand-new
    checker — nothing here has been verified against real TataNeu HTML yet.
    """
    html_lower = html.lower()
    logger.info(f"[tataneu][diag] HTML length={len(html)}, head={html[:200]!r}")

    logger.info(f"[tataneu][diag] OOS text {_OOS_TEXT!r} present: {_OOS_TEXT in html_lower}")
    for phrase in _IN_STOCK_TEXTS:
        logger.info(f"[tataneu][diag] in-stock text {phrase!r} present: {phrase in html_lower}")

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
                    logger.info(f"[tataneu][diag] JSON-LD availability={avail!r}")
    if not found_ld:
        logger.info("[tataneu][diag] JSON-LD availability: none found")

    oos_hits = [p for p in _OOS_PATTERNS if p in html_lower]
    logger.info(f"[tataneu][diag] generic OOS text patterns present: {oos_hits or 'none'}")

    btn_count = 0
    for el in soup.find_all(["button", "a"]):
        label = " ".join(filter(None, [
            el.get_text(" ", strip=True),
            el.get("aria-label", "") or "",
            el.get("data-testid", "") or "",
            " ".join(el.get("class", []) or []),
        ])).lower()
        if any(pat in label for pat in _ADD_PATTERNS) or label.strip() in ("add", "+"):
            logger.info(
                f"[tataneu][diag] cart/buy <{el.name}> "
                f"text={el.get_text(' ', strip=True)[:40]!r} class={el.get('class')} "
                f"disabled_attr={el.get('disabled')!r} aria-disabled={el.get('aria-disabled')!r} "
                f"→ _is_disabled={_is_disabled(el)}"
            )
            btn_count += 1
            if btn_count >= 10:
                break
    if btn_count == 0:
        logger.info("[tataneu][diag] no cart/buy button/anchor matched")


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    _log_diagnostics(soup, html)

    # ── Primary signals — explicit TataNeu phrases ───────────────────────────
    if _OOS_TEXT in html_lower:
        logger.info(f"[tataneu] OOS text '{_OOS_TEXT}' → False")
        return False

    for phrase in _IN_STOCK_TEXTS:
        if phrase in html_lower:
            logger.info(f"[tataneu] in-stock text '{phrase}' → True")
            return True

    # ── Fallback: JSON-LD ─────────────────────────────────────────────────────
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
                    logger.info("[tataneu] fallback JSON-LD: InStock → True")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[tataneu] fallback JSON-LD: OutOfStock/Discontinued → False")
                    return False
        except Exception:
            pass

    # ── Fallback: generic OOS text ────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[tataneu] fallback generic OOS text: '{pattern}' → False")
            return False

    # ── Fallback: button/anchor state (skip disabled) ─────────────────────────
    for el in soup.find_all(["button", "a"]):
        if _is_disabled(el):
            continue
        text = el.get_text(strip=True).lower()
        if text in ("add", "+") or any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[tataneu] fallback active <{el.name}> '{text[:40]}' → True")
            return True

    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if (
                "add-to-cart" in val or "addtocart" in val
                or "add-to-bag" in val or "addtobag" in val
                or any(p in val for p in _ADD_PATTERNS)
            ):
                logger.info(f"[tataneu] fallback active {attr}='{val[:40]}' → True")
                return True

    logger.info("[tataneu] no conclusive signal → defaulting OUT OF STOCK")
    return False
