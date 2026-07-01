"""
stock_checker.py
~~~~~~~~~~~~~~~~
Thin orchestration layer — delegates per-site logic to checkers/.
"""

import logging
import asyncio

import httpx
from bs4 import BeautifulSoup

from checkers import detect_site, build_scraper_url, HEADERS, CHECKER_MAP, PRICE_EXTRACTOR_MAP

logger = logging.getLogger(__name__)

# Re-export detect_site so existing imports from this module still work
__all__ = ["detect_site", "check_stock", "batch_check"]

# Sites that need JS rendering
_JS_SITES = {
    "flipkart", "zepto", "bigbasket", "blinkit", "croma", "instamart", "myntra",
    "jiomart", "reliancedigital",
}

# Quick-commerce sites where injecting a `pincode` cookie is attempted.
# Scrape.do's setCookies parameter forwards the cookie to the target site.
_PINCODE_COOKIE_SITES = frozenset({"bigbasket", "blinkit"})

# Quick-commerce sites that require storeId/session — simple cookie injection
# won't work; results reflect Scrape.do's IP geolocation instead.
_PINCODE_COMPLEX_SITES = frozenset({"zepto", "instamart"})

_QUICK_COMMERCE_SITES = _PINCODE_COOKIE_SITES | _PINCODE_COMPLEX_SITES


async def check_stock(url: str, site: str, pincode: str | None = None) -> tuple[bool, float | None]:
    """
    Returns (in_stock, current_price).
    current_price is only populated for sites in PRICE_EXTRACTOR_MAP (currently Amazon);
    it is None for all other sites and when extraction fails.
    """
    checker = CHECKER_MAP.get(site)
    if checker is None:
        logger.warning(f"No checker for site '{site}'")
        return False, None

    set_cookies = None
    if site in _QUICK_COMMERCE_SITES:
        if pincode:
            if site in _PINCODE_COOKIE_SITES:
                # Inject pincode as a cookie so the target site serves
                # location-specific stock rather than IP-geolocated defaults.
                set_cookies = f"pincode={pincode}"
                logger.info(f"[{site}] pincode={pincode!r} → setCookies={set_cookies!r}")
            else:
                logger.warning(
                    f"[{site}] pincode {pincode} saved but {site} requires "
                    f"a storeId/session lookup — results reflect Scrape.do IP "
                    f"geolocation, not delivery at pincode {pincode}"
                )
        else:
            logger.warning(
                f"[{site}] no pincode set — stock shown for Scrape.do IP "
                f"geolocation, not the user's delivery area. "
                f"Use /pins to add a pincode for more accurate results."
            )

    try:
        scraper_url = build_scraper_url(url, render_js=site in _JS_SITES, set_cookies=set_cookies)
        logger.info(f"[{site}] scraper_url (truncated)={scraper_url[:120]!r}")

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            timeout=60.0,
        ) as client:
            response = await client.get(scraper_url)
            response.raise_for_status()

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        result = checker(soup, html)

        price: float | None = None
        price_extractor = PRICE_EXTRACTOR_MAP.get(site)
        if price_extractor is not None:
            price = price_extractor(soup, html)

        price_str = f" @ ₹{price:,.0f}" if price is not None else ""
        logger.info(f"[{site}] {url} → {'IN STOCK' if result else 'OUT OF STOCK'}{price_str}")
        return result, price

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error {e.response.status_code} for {url}")
        return False, None
    except Exception as exc:
        logger.error(f"Error checking {url}: {exc}")
        return False, None


async def batch_check(
    products: list[dict],
    pincode: str | None = None,
) -> list[tuple[dict, bool]]:
    results = []
    for product in products:
        in_stock, _price = await check_stock(product["url"], product["site"], pincode=pincode)
        results.append((product, in_stock))
        await asyncio.sleep(3)
    return results
