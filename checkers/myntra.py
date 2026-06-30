import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to bag", "add to cart", "buy now", "add to wishlist and bag"]
_OOS_PATTERNS = [
    "sold out", "out of stock", "notify me",
    "currently out of stock", "size not available",
]

# Myntra-specific class name fragments
_PRICE_CLASSES = ["pdp-price", "product-discountedPrice", "pdp-mrp"]
_CART_CLASSES = ["pdp-add-to-bag", "add-to-bag", "addToBag"]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD structured data ───────────────────────────────────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[myntra] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail:
                    logger.info("[myntra] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Embedded JSON flags ───────────────────────────────────────────────────
    for true_key in ('"inStock":true', '"in_stock":true',
                     '"available":true', '"sizes_available":true'):
        if true_key in html:
            return True
    for false_key in ('"inStock":false', '"in_stock":false',
                      '"available":false', '"sizes_available":false'):
        if false_key in html:
            return False

    # ── Explicit OOS text ─────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[myntra] OOS signal: '{pattern}'")
            return False

    # ── Cart button classes ───────────────────────────────────────────────────
    for cls in _CART_CLASSES:
        if soup.find(attrs={"class": lambda c: c and cls in " ".join(c)}):
            return True

    # ── Button elements ───────────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            return True

    # ── data-testid / aria-label / id ────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Price element ─────────────────────────────────────────────────────────
    for cls in _PRICE_CLASSES:
        if soup.find(attrs={"class": lambda c: c and cls in " ".join(c)}):
            return True

    if "₹" in html and ("size" in html_lower or "delivery" in html_lower):
        return True

    logger.info("[myntra] no clear signal, defaulting OUT OF STOCK")
    return False
