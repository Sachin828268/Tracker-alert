import json
import logging
import re
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "buy now", "add to bag"]

_DELIVERY_RESTRICTION_PATTERNS = [
    "not available for your pincode",
    "not available for your location",
    "unfortunately not available for your location",
    "unfortunately not available",
]


def _normalized_text(soup: BeautifulSoup) -> str:
    """
    Collapse the page's visible text into a single whitespace-normalized,
    lowercased string. Croma often splits a phrase like "Not Available for
    your pincode" across several sibling tags (e.g. separate <span>s), so a
    plain substring check against soup.get_text() can miss it if get_text()
    preserves the original inter-tag whitespace/newlines. Normalizing here
    lets _DELIVERY_RESTRICTION_PATTERNS match regardless of markup structure.
    """
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).lower()


_OOS_PATTERNS = [
    "out of stock", "sold out", "currently unavailable",
    "notify me when available", "coming soon",
]
_CART_CLASSES = ["add-to-cart", "addToCart", "plp-add-to-cart"]


# Class tokens that mark a button/anchor as DISABLED. A bare "disabled" or
# "inactive" substring match catches most sites, but Croma's own OOS pages
# were observed disabling Buy Now / Add to Cart via CSS class alone (e.g.
# "disableBuyNow", "disableCartBtn") with no `disabled`/`aria-disabled` HTML
# attribute present at all — so those exact tokens are listed explicitly too.
# NOTE: Croma also has a *structural* hook class, "disable-btn-in-pdp", that
# appears on the button wrapper regardless of stock state (i.e. it's present
# on ACTIVE buttons too) — it is deliberately NOT included here. Matching a
# bare "disable" substring would false-positive on that class and mark
# in-stock products as OOS. If a future production log shows a genuinely
# disabled button whose only signal is an unlisted class, add that class
# explicitly rather than widening this to a generic "disable" substring.
_DISABLED_CLASS_MARKERS = ("disabled", "inactive", "disablebuynow", "disablecartbtn")


def _is_disabled(el) -> bool:
    """
    Return True if a BS4 element is visually/semantically disabled — via the
    `disabled` attribute, `aria-disabled="true"`, or one of
    _DISABLED_CLASS_MARKERS in its class list (see that constant's comment
    for why "disable-btn-in-pdp" is deliberately excluded).
    """
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


def _log_delivery_diagnostics(soup: BeautifulSoup, html: str) -> None:
    """
    Dump the decision trail to logs: delivery-restriction pattern hits (both
    raw-HTML and normalized-visible-text), keyword scans, delivery/pincode-ish
    elements, JSON-LD availability, OOS text matches, and buy/cart button
    state (class/attrs/disabled). This is log-only and never affects the
    returned value — it exists so a wrong assumption in the detection logic
    below is visible and correctable from real production logs (this is how
    the original Croma disabled-button and tag-split-pincode-text bugs were
    diagnosed and fixed) instead of being guessed at again.
    """
    html_lower = html.lower()
    text = _normalized_text(soup)
    logger.info(f"[croma][diag] HTML length={len(html)}, visible-text length={len(text)}")
    logger.info(f"[croma][diag] head: {html[:200]!r}")

    for p in _DELIVERY_RESTRICTION_PATTERNS:
        logger.info(
            f"[croma][diag] restriction {p!r}: in_html={p in html_lower} in_visible_text={p in text}"
        )

    for kw in ("not available", "unfortunately", "not serviceable"):
        idx = text.find(kw)
        if idx != -1:
            logger.info(f"[croma][diag] visible-text ...{text[max(0, idx - 60):idx + 90]!r}...")

    for kw in (
        "not available", "not serviceable", "unfortunately", "pincode",
        "pin code", "deliver by", "delivered by", "delivery at", "check delivery",
        "enter pincode", "enter your pincode", "notify me", "sold out",
    ):
        if kw in html_lower:
            logger.info(f"[croma][diag] keyword present: {kw!r}")

    hits = 0
    for el in soup.find_all(class_=True):
        cls = " ".join(el.get("class", [])).lower()
        if any(tok in cls for tok in ("deliver", "pincode", "serviceab", "availab", "location")):
            txt = el.get_text(" ", strip=True)[:120]
            logger.info(f"[croma][diag] el <{el.name}> class={el.get('class')} text={txt!r}")
            hits += 1
            if hits >= 25:
                logger.info("[croma][diag] (delivery-ish element dump capped at 25)")
                break

    for kw in ("not available", "unfortunately", "pincode", "notify me"):
        idx = html_lower.find(kw)
        if idx != -1:
            logger.info(f"[croma][diag] ...{html[max(0, idx - 90):idx + 90]!r}...")

    found_ld = False
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("offers") is not None:
                    avail = _offer_availability(item.get("offers", {}))
                    if avail:
                        found_ld = True
                        logger.info(f"[croma][diag] JSON-LD availability={avail!r}")
        except Exception:
            pass
    if not found_ld:
        logger.info("[croma][diag] JSON-LD availability: none found")

    for p in _OOS_PATTERNS:
        in_html = p in html_lower
        in_text = p in text
        if in_html or in_text:
            logger.info(f"[croma][diag] OOS pattern {p!r}: in_html={in_html} in_visible_text={in_text}")

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
                f"[croma][diag] buy/cart <{el.name}> "
                f"text={el.get_text(' ', strip=True)[:40]!r} class={el.get('class')} "
                f"disabled_attr={el.get('disabled')!r} aria-disabled={el.get('aria-disabled')!r} "
                f"style={el.get('style')!r} → _is_disabled={_is_disabled(el)}"
            )
            btn_count += 1
            if btn_count >= 10:
                break
    if btn_count == 0:
        logger.info("[croma][diag] no Buy Now / Add-to-Cart button matched")


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()
    text = _normalized_text(soup)

    _log_delivery_diagnostics(soup, html)

    # ── Delivery restriction — highest priority ─────────────────────────────
    # Checked before JSON-LD/buttons: a page can carry a stale/cached
    # "InStock" JSON-LD block and an enabled-looking Add to Cart button while
    # still showing "Not Available for your pincode" — the delivery gate is
    # the most trustworthy signal on record pages when it fires. Matched
    # against BOTH the normalized visible text (handles the phrase being
    # split across sibling tags, e.g. separate <span>s) and raw HTML.
    for pattern in _DELIVERY_RESTRICTION_PATTERNS:
        if pattern in text or pattern in html_lower:
            src = "visible-text" if pattern in text else "raw-html"
            logger.info(f"[croma] delivery restriction ({src}): '{pattern}' → False")
            return False

    # Scoped delivery-element check — catches delivery/pincode-labelled
    # elements whose restriction text didn't match the patterns above
    # verbatim (e.g. differently worded serviceability messages).
    for el in soup.find_all(class_=True):
        cls = " ".join(el.get("class", [])).lower()
        if not any(tok in cls for tok in ("deliver", "serviceab", "pincode", "availab")):
            continue
        etxt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).lower()
        if any(sig in etxt for sig in ("not available", "unfortunately", "not serviceable")):
            logger.info(
                f"[croma] delivery element <{el.name}> class={el.get('class')} "
                f"text={etxt[:120]!r} → False"
            )
            return False

    # ── JSON-LD pass ─────────────────────────────────────────────────────────
    # InStock is deferred rather than returned immediately: JSON-LD on Croma
    # has been observed stale (still says InStock after the product actually
    # went OOS), so it is only trusted as a last-resort fallback below, after
    # the button scan has had a chance to override it. OutOfStock/Discontinued
    # is trusted immediately since a false negative there is safe (rare) and a
    # site would not label a genuinely in-stock product as OutOfStock.
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
                    logger.info("[croma] JSON-LD: InStock (deferred)")
                    json_ld_in_stock = True
                elif "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[croma] JSON-LD: OutOfStock → False")
                    return False
        except Exception:
            pass

    # ── OOS text patterns ─────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[croma] OOS text: '{pattern}' → False")
            return False

    # ── Button state ──────────────────────────────────────────────────────────
    cart_buttons = []
    seen_ids = set()

    def _add_candidate(el):
        if id(el) not in seen_ids:
            seen_ids.add(id(el))
            cart_buttons.append(el)

    for cls in _CART_CLASSES:
        for el in soup.find_all(class_=cls):
            _add_candidate(el)
    for el in soup.find_all(["button", "a"]):
        if any(p in el.get_text(strip=True).lower() for p in _ADD_PATTERNS):
            _add_candidate(el)
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            if any(p in (el.get(attr) or "").lower() for p in _ADD_PATTERNS):
                _add_candidate(el)

    if cart_buttons:
        active = [b for b in cart_buttons if not _is_disabled(b)]
        if active:
            el = active[0]
            logger.info(
                f"[croma] active buy/cart <{el.name}> "
                f"text={el.get_text(' ', strip=True)[:40]!r} class={el.get('class')} → True"
            )
            return True
        logger.info(
            f"[croma] all {len(cart_buttons)} buy/cart button(s) disabled "
            f"(overrides JSON-LD InStock={json_ld_in_stock}) → False"
        )
        return False

    # ── Final fallback: only trust JSON-LD if NO buttons found at all. If we
    # found buttons, the block above already returned — this is reached only
    # when the button scan found nothing to check, so a lingering InStock
    # JSON-LD is the best remaining signal.
    if json_ld_in_stock and not cart_buttons:
        logger.info("[croma] JSON-LD InStock confirmed → True")
        return True

    logger.info("[croma] no conclusive signal → False")
    return False
