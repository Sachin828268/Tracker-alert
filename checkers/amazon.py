import json
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def extract_price(soup: BeautifulSoup, html: str) -> float | None:
    """
    Extract the current listed price from an Amazon product page.
    Returns None if the price cannot be determined.
    """
    # JSON-LD is the most reliable source — it's product-scoped and structured.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                offers = item.get("offers", {})
                if isinstance(offers, dict):
                    price = offers.get("price")
                    if price is not None:
                        p = float(price)
                        logger.info(f"[amazon] JSON-LD price: ₹{p}")
                        return p
        except Exception:
            pass

    # DOM fallback: a-price-whole + a-price-fraction (e.g. "1,299" + "00")
    whole_el = soup.find("span", {"class": "a-price-whole"})
    if whole_el:
        try:
            whole = whole_el.get_text(strip=True).replace(",", "").rstrip(".")
            frac_el = soup.find("span", {"class": "a-price-fraction"})
            frac = frac_el.get_text(strip=True) if frac_el else "00"
            p = float(f"{whole}.{frac}")
            logger.info(f"[amazon] DOM price: ₹{p}")
            return p
        except Exception:
            pass

    logger.info("[amazon] price not found in page")
    return None


def check(soup: BeautifulSoup, html: str) -> bool:
    html_lower = html.lower()

    avail = soup.find("div", {"id": "availability"})
    if avail:
        text = avail.get_text(" ", strip=True).lower()
        logger.info(f"[amazon] availability text: {text}")
        if "currently unavailable" in text or "out of stock" in text:
            return False
        if "in stock" in text or "available" in text:
            return True

    if soup.find("div", {"id": "outOfStock"}):
        return False

    if soup.find("input", {"id": "add-to-cart-button"}):
        return True
    if soup.find("input", {"id": "buy-now-button"}):
        return True
    if soup.find("input", {"name": "submit.add-to-cart"}):
        return True

    if "currently unavailable" in html_lower:
        return False
    if "add to cart" in html_lower:
        return True
    if "buy now" in html_lower:
        return True

    if soup.find("span", {"class": "a-price-whole"}):
        return True

    logger.info("[amazon] no signal found")
    return False
