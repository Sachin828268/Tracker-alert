import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

_ADD_PATTERNS = ["add to cart", "add to basket", "add", "buy now"]
_OOS_PATTERNS = [
    "notify me", "out of stock", "sold out",
    "currently unavailable", "not available",
]


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
                    logger.info("[bigbasket] JSON-LD: InStock")
                    return True
                if "OutOfStock" in avail:
                    logger.info("[bigbasket] JSON-LD: OutOfStock")
                    return False
        except Exception:
            pass

    # ── Embedded JSON flags ───────────────────────────────────────────────────
    for true_key in ('"in_stock": true', '"in_stock":true', '"inStock":true'):
        if true_key in html:
            return True
    for false_key in ('"in_stock": false', '"in_stock":false', '"inStock":false'):
        if false_key in html:
            return False

    # ── Explicit OOS text ─────────────────────────────────────────────────────
    for pattern in _OOS_PATTERNS:
        if pattern in html_lower:
            logger.info(f"[bigbasket] OOS signal: '{pattern}'")
            return False

    # ── Button elements ───────────────────────────────────────────────────────
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if any(p in text for p in _ADD_PATTERNS):
            return True

    # ── data-testid / aria-label ──────────────────────────────────────────────
    for attr in ("data-testid", "aria-label", "id"):
        for el in soup.find_all(attrs={attr: True}):
            val = (el.get(attr) or "").lower()
            if any(p in val for p in _ADD_PATTERNS):
                return True

    # ── Price element present ─────────────────────────────────────────────────
    price = soup.find(attrs={"class": lambda c: c and "price" in c.lower()})
    if price:
        return True

    logger.info("[bigbasket] no clear signal, defaulting OUT OF STOCK")
    return False
