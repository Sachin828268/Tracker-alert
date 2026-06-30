from .common import detect_site, build_scraper_url, HEADERS
from . import amazon, flipkart, zepto, bigbasket, blinkit, croma, instamart, myntra

CHECKER_MAP = {
    "amazon":    amazon.check,
    "flipkart":  flipkart.check,
    "zepto":     zepto.check,
    "bigbasket": bigbasket.check,
    "blinkit":   blinkit.check,
    "croma":     croma.check,
    "instamart": instamart.check,
    "myntra":    myntra.check,
}

__all__ = ["detect_site", "build_scraper_url", "HEADERS", "CHECKER_MAP"]
