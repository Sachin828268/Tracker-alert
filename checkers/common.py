"""Shared utilities for all site checkers."""

import logging
import os
from urllib.parse import urlparse, urlencode

from config import SUPPORTED_SITES

logger = logging.getLogger(__name__)

SCRAPINGDOG_API_URL = "https://api.scrapingdog.com/scrape"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def detect_site(url: str) -> str | None:
    host = urlparse(url).netloc.lower().replace("www.", "")
    for site_key, domains in SUPPORTED_SITES.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return site_key
    return None


def build_scraper_url(url: str, render_js: bool = False, set_cookies: str | None = None) -> str:
    # Read at call time so Railway's runtime env var is always used,
    # regardless of when this module was first imported.
    api_key = os.environ.get("SCRAPINGDOG_KEY", "")
    params = {
        "api_key": api_key,
        "url": url,
        "dynamic": "true" if render_js else "false",
        "country": "in",
    }
    if set_cookies:
        # Scrapingdog has no setCookies equivalent — cookie injection for
        # pincode-specific stock (BigBasket, Blinkit) is not supported;
        # results will reflect proxy geolocation instead.
        logger.warning(
            f"[scrapingdog] setCookies requested ({set_cookies!r}) but Scrapingdog "
            f"does not support cookie injection — stock may reflect proxy geolocation"
        )
    return f"{SCRAPINGDOG_API_URL}?{urlencode(params)}"
