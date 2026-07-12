import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Page is JS-rendered (stock text is injected client-side) — the real render
# switch is stock_checker._JS_SITES; this flag is documentation-only.
NEEDS_JS = True

# Deliberately ONLY these two keywords, checked against the page's visible
# text (HTML tags/scripts stripped) — no JSON-LD, no embedded JSON, no
# button/class scanning. Simpler and requested explicitly in place of the
# earlier button/attribute-based heuristics.
_OOS_KEYWORDS = ["notify me", "out of stock"]


def _visible_text(html: str) -> str:
    """Parse html fresh (rather than reusing the caller's `soup`, so this
    never mutates it) and strip <script>/<style> content before extracting
    text — otherwise get_text() would also pick up any of these keywords if
    they happened to appear inside inline JS, which isn't "visible text"."""
    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style"]):
        tag.decompose()
    return text_soup.get_text(" ", strip=True)


def check(soup: BeautifulSoup, html: str) -> bool:
    visible_text = _visible_text(html).lower()

    hits = [kw for kw in _OOS_KEYWORDS if kw in visible_text]
    if hits:
        logger.info(f"[oneplus] visible-text keyword(s) found {hits} → False (out of stock)")
        return False

    logger.info("[oneplus] no OOS keyword in visible text → True (in stock)")
    return True
