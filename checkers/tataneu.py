import re
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEEDS_JS = True

# Sole detection signal: TataNeu's delivery-estimate text "Get it by <day>
# <Month>" (e.g. "Get it by 8 Jul", "Get it by 15 Aug") only renders when the
# product can actually be delivered to the resolved location — so its
# presence means IN STOCK, its absence means OUT OF STOCK. Based on real
# visual confirmation of live pages:
#   in-stock  → "Standard • Get it by 8 Jul!"
#   out-of-stock → "The product is not available" with no delivery estimate
#
# Unlike the earlier "currently"-word approach, this defaults to OUT OF STOCK
# when the signal is absent (a fetch glitch / error page / blocked response
# has no delivery estimate → OOS), matching every other checker's safe
# default: a false "back in stock" alert is worse than a missed one.
#
# The month alternation uses 3-letter prefixes, which match both abbreviations
# ("Jul") and full names ("July" = "jul" + "y") since every English month name
# begins with its standard 3-letter abbreviation.
_DELIVERY_ESTIMATE_RE = re.compile(
    r"get it by\s+\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    re.IGNORECASE,
)


def _normalized_text(soup: BeautifulSoup) -> str:
    """Whitespace-collapsed visible text. Matched in addition to raw HTML so
    the delivery estimate is still found if TataNeu splits it across sibling
    tags (e.g. <span>Get it by</span> <span>8 Jul</span>) — the raw HTML would
    have tags between the words, defeating the regex's \\s+, but get_text()
    joins them with spaces. This is the tag-split failure mode seen on Croma
    earlier; checking both is strictly more robust with no downside."""
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _find_delivery_estimate(soup: BeautifulSoup, html: str):
    """Return a regex Match for the delivery estimate from either the raw HTML
    or the normalized visible text, or None. Raw HTML is tried first so the
    logged context reflects the actual page markup when possible."""
    return _DELIVERY_ESTIMATE_RE.search(html) or _DELIVERY_ESTIMATE_RE.search(_normalized_text(soup))


def _log_diagnostics(soup: BeautifulSoup, html: str) -> None:
    """Log-only decision trail: whether the delivery-estimate pattern matched
    and the exact text it matched, so real production traffic reveals whether
    the pattern is firing as expected (or matching something unintended)."""
    logger.info(f"[tataneu][diag] HTML length={len(html)}, head={html[:200]!r}")
    in_raw = _DELIVERY_ESTIMATE_RE.search(html)
    in_text = _DELIVERY_ESTIMATE_RE.search(_normalized_text(soup))
    m = in_raw or in_text
    if m:
        src = "raw-html" if in_raw else "visible-text (tag-split)"
        logger.info(f"[tataneu][diag] delivery-estimate matched ({src}): {m.group(0)!r}")
    else:
        logger.info("[tataneu][diag] delivery-estimate 'Get it by <day> <Month>': not found")


def check(soup: BeautifulSoup, html: str) -> bool:
    _log_diagnostics(soup, html)

    if _find_delivery_estimate(soup, html):
        logger.info("[tataneu] delivery estimate present → IN STOCK")
        return True

    logger.info("[tataneu] no delivery estimate → OUT OF STOCK")
    return False
