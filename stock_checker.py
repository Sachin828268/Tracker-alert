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

# Sites where injecting `pincode=<value>` as a cookie causes the target site to
# serve location-specific stock data. Blinkit reads this cookie directly.
_PINCODE_COOKIE_SITES = frozenset({"blinkit"})

# Sites that require session/storeId for location-specific stock — simple cookie
# injection won't work; results reflect Scrape.do's IP geolocation instead.
#
# BigBasket: location is stored server-side in a Django session tied to a
# logged-in account (addr_id). A bare `pincode=<value>` cookie is NOT read by
# BigBasket for stock determination. Injecting it could bypass the location gate
# and let the page render, but the stock shown would be for Scrape.do's proxy IP
# (typically a metro city), not the user's pincode. This is worse than letting
# the location gate fire (which the checker correctly treats as OOS).
_PINCODE_COMPLEX_SITES = frozenset({"zepto", "instamart", "bigbasket"})

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
                if site == "bigbasket":
                    logger.warning(
                        f"[bigbasket] pincode {pincode} saved but BigBasket uses "
                        f"server-side session location (Django sessionid + addr_id). "
                        f"A bare pincode= cookie is NOT read for stock data — previously "
                        f"injecting it bypassed BigBasket's location gate and returned "
                        f"stock for Scrape.do's proxy IP (not pincode {pincode}), causing "
                        f"false in-stock alerts. Cookie injection removed. Stock will now "
                        f"reflect proxy geolocation or trigger the location gate (→ OOS)."
                    )
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
        logger.info(f"[{site}] setCookies={set_cookies!r} render_js={site in _JS_SITES}")
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
