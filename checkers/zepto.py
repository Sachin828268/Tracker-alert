import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to bag"]
_OOS_PATTERNS = ["out of stock", "sold out", "notify me when available", "notify me"]

# Class tokens that mark a button/anchor as DISABLED via CSS alone, with no
# `disabled`/`aria-disabled` HTML attribute present — the same pattern
# discovered on Croma (see checkers/croma.py's history). A real risk here
# specifically because Zepto's own cart button reuses the SAME short
# "ADD"/"+" label for both the in-stock and out-of-stock states, differing
# only by styling. No known structural class collision has been observed for
# Zepto, so the broader "disable" substring is used as-is; if a production
# log ever shows an active button being misflagged, add the offending class
# to an explicit exclusion rather than narrowing this.
_DISABLED_CLASS_MARKERS = ("disable", "inactive")

# Zepto uses a storeId (derived from exact coordinates) for availability.
# A simple pincode cookie is insufficient — true pincode-specific checking
# requires a storeId lookup call. Until that is implemented, results reflect
# Scrape.do's IP geolocation.
_LOCATION_GATE_SIGNALS = [
    "enter your pincode",
    "enter pincode",
    "select your location",
    "please enter your location",
    "add a delivery address",
]


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


def _log_diagnostics(soup: BeautifulSoup, html: str) -> None:
    """
    Dump the decision trail to logs: location-gate hits, JSON-LD
    availability, OOS text matches, and every candidate Add/+ button's
    text/class/computed-disabled state (whether or not it ends up being the
    chosen signal). Log-only: never changes the returned value.
    """
    html_lower = html.lower()
    logger.info(f"[zepto][diag] HTML length={len(html)}, head={html[:200]!r}")

    gate_hits = [s for s in _LOCATION_GATE_SIGNALS if s in html_lower]
    logger.info(f"[zepto][diag] location-gate signals present: {gate_hits or 'none'}")

    found_ld = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("offers"):
                    avail = item.get("offers", {}).get("availability", "")
                    if avail:
                        found_ld = True
                        logger.info(f"[zepto][diag] JSON-LD availability={avail!r}")
        except Exception:
            pass
    if not found_ld:
        logger.info("[zepto][diag] JSON-LD availability: none found")

    oos_hits = [p for p in _OOS_PATTERNS if p in html_lower]
    logger.info(f"[zepto][diag] OOS text patterns present: {oos_hits or 'none'}")

    btn_count = 0
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if text in ("add", "+") or any(p in text for p in _ADD_PATTERNS):
            logger.info(
                f"[zepto][diag] cart <button> text={btn.get_text(strip=True)[:40]!r} "
                f"class={btn.get('class')} disabled_attr={btn.get('disabled')!r} "
                f"aria-disabled={btn.get('aria-disabled')!r} → _is_disabled={_is_disabled(btn)}"
            )
            btn_count += 1
            if btn_count >= 10:
                break
    if btn_count == 0:
        logger.info("[zepto][diag] no ADD/+ button matched")


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    _log_diagnostics(soup, html)

    # ── Location gate (no delivery area resolved) ─────────────────────────────
    if any(sig in html_lower for sig in _LOCATION_GATE_SIGNALS):
        logger.warning(
            "[zepto] location gate detected — pincode-specific stock unavailable "
            "(Zepto requires storeId); returning OOS"
        )
        return False

    # ── JSON-LD (most reliable — scoped to the specific product) ─────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[zepto] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail:
                    logger.info("[zepto] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Negative signals (checked before buttons — related-product "Add" buttons
    # can appear on OOS pages and would otherwise trigger a false positive) ────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[zepto] OOS signal: '{pattern}'")
            return False

    # ── Positive signals: "Add to Cart" / "Add to Bag" button ────────────────
    # Zepto shows "Notify Me" (not Add to Cart) when OOS — so if we find an
    # actual, non-disabled add-to-cart button or link, the product is in stock.
    for btn in soup.find_all("button"):
        if _is_disabled(btn):
            continue
        text = btn.get_text(strip=True).lower()
        if text in ("add", "+"):  # zepto's minimal cart button
            logger.info("[zepto] active ADD/+ button found")
            return True
        if any(p in text for p in _ADD_PATTERNS):
            logger.info(f"[zepto] active add pattern in button '{text[:40]}'")
            return True

    for attr in ("data-testid", "aria-label"):
        for el in soup.find_all(attrs={attr: True}):
            if _is_disabled(el):
                continue
            val = (el.get(attr) or "").lower()
            if "add-to-cart" in val or "addtocart" in val or any(p in val for p in _ADD_PATTERNS):
                logger.info(f"[zepto] active {attr}='{val[:40]}'")
                return True

    logger.info("[zepto] no signal, defaulting OUT OF STOCK")
    return False
