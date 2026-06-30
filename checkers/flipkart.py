import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Flipkart's price/cart widgets are injected by JS — rendering is required
NEEDS_JS = True

# Price class names across Flipkart UI versions
_PRICE_CLASSES = [
    "_30jeq3", "Nx9bqj", "_25b18c", "_16Jk6d", "CxhGGd",  # legacy
    "hl05eU", "_4b5DiR", "x+jhYQ", "yRaY8j", "_1vC4OE",   # newer
]

_ADD_PATTERNS = ["add to cart", "add to bag", "buy now"]
_OOS_PATTERNS = ["sold out", "currently unavailable", "notify me when available"]


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    # ── JSON-LD structured data (most reliable when present) ─────────────────
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                avail = item.get("offers", {}).get("availability", "")
                if "InStock" in avail:
                    logger.info("[flipkart] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail or "Discontinued" in avail:
                    logger.info("[flipkart] JSON-LD: OutOfStock/Discontinued")
                    return False
        except Exception:
            pass

    # ── Strong negative signals ───────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[flipkart] negative signal: '{pattern}'")
            return False

    if "out of stock" in html_lower and not any(p in html_lower for p in _ADD_PATTERNS):
        logger.info("[flipkart] 'out of stock' found, no add-to-cart")
        return False

    # ── Button elements ───────────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        if any(p in btn.get_text(strip=True).lower() for p in _ADD_PATTERNS):
            return True

    # ── data-testid / aria-label / id attributes ──────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Text signals ─────────────────────────────────────────────────────────
    if any(p in html_lower for p in _ADD_PATTERNS):
        return True

    # ── Price element classes ─────────────────────────────────────────────────
    for cls in _PRICE_CLASSES:
        if soup.find(["div", "span"], {"class": cls}):
            return True

    # ── Generic rupee + delivery context ─────────────────────────────────────
    if "₹" in html and ("pincode" in html_lower or "delivery" in html_lower):
        return True

    logger.info("[flipkart] no clear signal found, defaulting OUT OF STOCK")
    return False
