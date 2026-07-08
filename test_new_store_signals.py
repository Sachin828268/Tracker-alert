#!/usr/bin/env python3
"""
Diagnostic probe for candidate NEW stores (Vijay Sales, Vivo, iQOO, Oppo,
Unicorn Store) — run in the Railway Console, where SCRAPEDO_KEY is set. Does
NOT touch the bot, database, or any tracked products.

Purpose: before writing any checker, SEE the real page structure — is the site
reachable through Scrape.do, does it need JS rendering, and which stock signal
(JSON-LD availability, OOS text, add-to-cart button state, Shopify
"available" flag, …) is actually present. This mirrors the _log_diagnostics
pattern used in checkers/bigbasket.py etc., but standalone.

Usage — pass one or more REAL product URLs, ideally one confirmed OUT-OF-STOCK
and one confirmed IN-STOCK per store so we can compare what changes:

    python3 test_new_store_signals.py \
        "https://shop.unicornstore.in/products/<oos-item>" \
        "https://shop.unicornstore.in/products/<in-stock-item>" \
        "https://www.vijaysales.com/<some-product>" \
        "https://www.vivo.com/in/products/x300-ultra"

For each URL it fetches via Scrape.do TWICE (render=false = 1 credit, then
render=true = 5 credits) and prints the candidate signals for each, so we can
pick the cheapest render mode that still exposes a reliable signal.
"""

import asyncio
import json
import re
import sys

import httpx
from bs4 import BeautifulSoup

from checkers.common import build_scraper_url, HEADERS

_OOS_PATTERNS = [
    "out of stock", "out-of-stock", "sold out", "currently unavailable",
    "notify me", "coming soon", "temporarily unavailable", "not available",
]
_POS_PATTERNS = [
    "add to cart", "add to bag", "add to basket", "buy now", "in stock",
    "add to wishlist and buy", "shop now",
]
_ADD_BUTTON_PHRASES = (
    "add to cart", "add to bag", "add to basket", "buy now", "add to wishlist and buy",
    "shop now", "order now", "pre-order", "preorder", "buy", "purchase",
)


def _normalize_label(s: str) -> str:
    """Lowercase-and-strip-separators form, so 'Add To Cart', 'add-to-cart',
    and 'addToCart' (once lowercased: 'addtocart') all compare equal. A plain
    substring check against phrases WITH spaces (e.g. "add to cart") silently
    misses camelCase/CSS-module class names with no spaces at all — a common
    pattern on custom-built SPA storefronts."""
    return re.sub(r"[\s\-_]+", "", s)


_ADD_BUTTON_PHRASES_NORM = [_normalize_label(p) for p in _ADD_BUTTON_PHRASES]

_PLATFORM_MARKERS = {
    "Shopify": ["cdn.shopify.com", "shopify.theme", "/cdn/shop/", "myshopify", "shopify-section"],
    "Magento/Adobe": ["mage/", "magento", "/static/version", "adobe commerce", "mage-init", "catalog-product"],
    "Next.js/React SPA": ["__next_data__", "_next/static", "window.__nuxt__", "data-reactroot"],
}


def _jsonld_availability(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []

    def _avails(o) -> list[str]:
        r: list[str] = []
        if isinstance(o, dict):
            if o.get("availability"):
                r.append(str(o["availability"]))
            if o.get("offers"):
                r += _avails(o["offers"])
        elif isinstance(o, list):
            for x in o:
                r += _avails(x)
        return r

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("offers"):
                found += _avails(item.get("offers"))
    return found


# Class-name substrings that commonly mark a dedicated stock-status element on
# custom storefronts (esp. Magento PWA / BEM-style themes like Vijay Sales' —
# "button__root_highPriority", "product__price--addtocart" are exactly this
# naming convention). A structural status element (e.g.
# <div class="product__stock--outOfStock">Out of Stock</div>) is a far more
# reliable signal than page-wide text search or button disabled-state, which
# can pick up unrelated widgets or hydration-timing noise.
_STOCK_CLASS_MARKERS = ("stock", "availability", "in-stock", "out-of-stock", "outofstock", "unavailable")


def _ancestor_classes(el, levels: int = 3) -> list[str]:
    """Class lists of up to `levels` parent elements, innermost first — helps
    spot a wrapping stock-status container around a button."""
    chain = []
    node = el.parent
    for _ in range(levels):
        if node is None or not hasattr(node, "get"):
            break
        cls = node.get("class")
        if cls:
            chain.append(" ".join(cls))
        node = node.parent
    return chain


def _text_context(html_text: str, pattern: str, window: int = 120) -> str:
    """~window chars of VISIBLE-text context around the first occurrence of
    `pattern`, so we can see whether an OOS/positive phrase sits near the buy
    box or in an unrelated part of the page (e.g. a related-products widget)."""
    idx = html_text.lower().find(pattern)
    if idx == -1:
        return ""
    start, end = max(0, idx - window), idx + len(pattern) + window
    return "…" + html_text[start:end].replace("\n", " ").strip() + "…"


# Magento/Adobe Commerce's own internal GraphQL + MSI (Multi-Source Inventory)
# field names — distinct vocabulary from the generic OOS-text / button-state /
# Shopify "available" signals already checked, and from JSON-LD (which Vijay
# Sales' probe showed is static/stale). A PWA storefront commonly embeds the
# initial GraphQL query result as JSON in the page for SSR hydration (the same
# pattern as Next.js's __NEXT_DATA__) — if so, the REAL live stock data may be
# here even though the static button/text scaffolding isn't.
_MAGENTO_STOCK_PATTERNS = [
    (r'"stock_status"\s*:\s*"(IN_STOCK|OUT_OF_STOCK)"', "stock_status"),
    (r'"is_salable"\s*:\s*(true|false)', "is_salable"),
    (r'"salable_quantity"\s*:\s*(\d+)', "salable_quantity"),
    (r'"is_in_stock"\s*:\s*(true|false)', "is_in_stock"),
]


# Numeric id tokens are more useful when the id LOOKS like a real Magento
# entity/SKU id (not a stray 1-2 digit number) — so only 5+ digit sequences
# from the URL are used for correlation.
_ID_TOKEN_RE = re.compile(r"\d{5,}")


def _json_object_span(html: str, start: int) -> tuple[int, int] | None:
    """
    Given the position of an opening '{', return (start, end-exclusive) of its
    matching closing '}' via a depth-counting scan that skips brace characters
    inside string literals (tracking backslash-escapes so an escaped quote
    doesn't end the string early). Returns None if unbalanced/truncated.
    """
    if start >= len(html) or html[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    i = start
    while i < len(html):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    return None


def _own_product_object(html: str, id_tok: str) -> tuple[int, int] | None:
    """
    Find the JSON value object that BELONGS to a given id token — i.e. the
    {...} immediately following a `"...<id_tok>...": ` key — rather than just
    "nearby text", which a fixed character-offset window gets wrong as soon as
    a product's own object is longer than the window (confirmed via a
    realistic-scale synthetic test: a 400-char window mis-attributed a
    NEIGHBORING product's IN_STOCK to the target OOS product, and missed the
    target's own OUT_OF_STOCK entirely, because Apollo/GraphQL cache entries
    routinely exceed 400 chars once padded with name/price/image/etc. fields).
    Brace-matching properly bounds the search to exactly that product's own
    object regardless of its length or how much sibling-entry noise surrounds
    it. Returns None if no id occurrence is followed by a `{` within a short
    lookahead (i.e. this id isn't actually a JSON object key here).
    """
    for m in re.finditer(re.escape(id_tok), html):
        # Look for '{' within a short lookahead after the id (covers
        # `"ProductInterface:245180":{` and similar key-then-colon-then-brace
        # patterns) — NOT a big window, just enough to skip the closing quote/colon.
        lookahead = html[m.end():m.end() + 30]
        brace_offset = lookahead.find("{")
        if brace_offset == -1:
            continue
        span = _json_object_span(html, m.end() + brace_offset)
        if span:
            return span
    return None


def _magento_stock_fields(html: str, url: str = "") -> list[str]:
    """
    Deduplicated `key=value (xN)` counts for each Magento stock field found
    ANYWHERE in the raw HTML/embedded-JS, PLUS id-correlated matches scoped to
    exactly the product's OWN JSON object (via _own_product_object) — a plain
    page-wide count alone can't tell which stock_status belongs to THIS
    product vs. a related-products/recommendation carousel embedding several
    other products' data too.
    """
    from collections import Counter
    counts = Counter()
    for pattern, label in _MAGENTO_STOCK_PATTERNS:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            counts[f"{label}={m.group(1)}"] += 1

    correlated = []
    for id_tok in set(_ID_TOKEN_RE.findall(url)):
        span = _own_product_object(html, id_tok)
        if span is None:
            continue
        own_json = html[span[0]:span[1]]
        for pattern, label in _MAGENTO_STOCK_PATTERNS:
            for m in re.finditer(pattern, own_json, re.IGNORECASE):
                correlated.append(f"{label}={m.group(1)}  [OWN PRODUCT — id {id_tok!r}'s own JSON object]")

    out = [f"{k} (×{v})" for k, v in counts.most_common(12)]
    out += sorted(set(correlated))
    return out


def _probe(html: str, url: str = "") -> dict:
    low = html.lower()
    soup = BeautifulSoup(html, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    platforms = [name for name, marks in _PLATFORM_MARKERS.items() if any(m in low for m in marks)]

    # Clickable candidates: semantic button/input/a tags PLUS div/span elements
    # that behave as buttons via role="button" or an onclick handler — custom
    # SPA storefronts (a brand site like Oppo is a likely example) very
    # commonly implement "buttons" this way instead of semantic <button> tags,
    # which a tag-scoped scan would silently miss entirely.
    seen_ids: set[int] = set()
    clickable_els = []
    for el in (
        soup.find_all(["button", "input", "a"])
        + soup.find_all(["div", "span"], attrs={"role": "button"})
        + soup.find_all(["div", "span"], onclick=True)
    ):
        if id(el) not in seen_ids:
            seen_ids.add(id(el))
            clickable_els.append(el)

    buttons = []              # matched a known buy-phrase (strict signal)
    loose_candidates = []     # any other short-text clickable element, for
                               # manual review in case the real wording isn't
                               # one we anticipated at all
    for el in clickable_els:
        text = el.get_text(" ", strip=True)
        label = " ".join(filter(None, [
            text,
            el.get("value", "") or "",
            el.get("name", "") or "",
            el.get("aria-label", "") or "",   # icon-only buttons have no visible text
            el.get("data-testid", "") or "",
            " ".join(el.get("class", []) or []),
        ])).lower()
        norm = _normalize_label(label)
        is_buy_like = (
            any(p in label for p in _ADD_BUTTON_PHRASES)
            or any(p in norm for p in _ADD_BUTTON_PHRASES_NORM)
        )
        if is_buy_like and len(label) < 150:
            buttons.append(
                f"<{el.name}> role={el.get('role')!r} onclick={'yes' if el.get('onclick') else 'no'} "
                f"disabled={el.get('disabled')!r} aria-disabled={el.get('aria-disabled')!r} "
                f"class={el.get('class')} aria-label={el.get('aria-label')!r} "
                f"text={text[:35]!r} ancestors={_ancestor_classes(el)}"
            )
        elif 0 < len(text) < 40:
            loose_candidates.append(f"<{el.name}> text={text!r} class={el.get('class')}")
    buttons = buttons[:10]
    loose_candidates = loose_candidates[:15]

    # Structural stock-status elements: any element whose OWN class (not text)
    # contains a stock-related marker. Printed regardless of what it says, so
    # we can see the class naming convention even if the displayed word isn't
    # "stock" (e.g. a class like "product__availability--out" wrapping "Notify Me").
    stock_class_elements = []
    for el in soup.find_all(class_=True):
        classes = " ".join(el.get("class", [])).lower()
        if any(m in classes for m in _STOCK_CLASS_MARKERS):
            text = el.get_text(" ", strip=True)
            if 0 < len(text) < 60:  # skip huge container divs; want the leaf status text
                stock_class_elements.append(f"class={el.get('class')} text={text!r}")
            if len(stock_class_elements) >= 8:
                break

    oos_hits = [p for p in _OOS_PATTERNS if p in low]
    positive_hits = [p for p in _POS_PATTERNS if p in low]

    return {
        "platforms": platforms,
        "jsonld_availability": _jsonld_availability(soup),
        "shopify_available_flags": re.findall(r'"available"\s*:\s*(true|false)', low)[:8],
        "magento_stock_fields": _magento_stock_fields(html, url),
        "oos_text": oos_hits,
        "oos_text_context": {p: _text_context(visible_text, p) for p in oos_hits},
        "positive_text": positive_hits,
        "positive_text_context": {p: _text_context(visible_text, p) for p in positive_hits},
        "stock_class_elements": stock_class_elements,
        "buttons": buttons,
        "loose_candidates": loose_candidates,
    }


async def _fetch(url: str, render: bool) -> tuple[int, str]:
    scraper_url = build_scraper_url(url, render_js=render)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=90.0) as client:
        r = await client.get(scraper_url)
    return r.status_code, r.text


async def _run(url: str) -> None:
    print("\n" + "=" * 80)
    print(f"URL: {url}")
    for render in (False, True):
        print(f"\n--- render={render} ({'5 credits' if render else '1 credit'}) ---")
        try:
            status, html = await _fetch(url, render)
        except Exception as exc:
            print(f"  FETCH FAILED: {exc}")
            continue
        print(f"  HTTP {status}, HTML length={len(html)}")
        if status != 200 or len(html) < 500:
            print("  ⚠️  likely blocked / challenge / empty — reachability problem")
        info = _probe(html, url)
        print(f"  platform markers      : {info['platforms'] or 'none detected'}")
        print(f"  JSON-LD availability  : {info['jsonld_availability'] or 'NONE'}")
        print(f"  shopify available flag: {info['shopify_available_flags'] or 'none'}")
        print(f"  magento stock fields  : {info['magento_stock_fields'] or 'none'}")
        print(f"  OOS text present      : {info['oos_text'] or 'none'}")
        for p, ctx in info["oos_text_context"].items():
            print(f"      context for {p!r}: {ctx}")
        print(f"  positive text present : {info['positive_text'] or 'none'}")
        for p, ctx in info["positive_text_context"].items():
            print(f"      context for {p!r}: {ctx}")
        print(f"  stock-status class elements ({len(info['stock_class_elements'])}):")
        for s in info["stock_class_elements"]:
            print(f"      {s}")
        print(f"  add/buy buttons ({len(info['buttons'])}):")
        for b in info["buttons"]:
            print(f"      {b}")
        print(f"  loose clickable candidates, no known phrase matched ({len(info['loose_candidates'])}):")
        for c in info["loose_candidates"]:
            print(f"      {c}")


async def main() -> None:
    urls = sys.argv[1:]
    if not urls:
        print("Pass one or more real product URLs as arguments (see the module docstring).")
        return
    for url in urls:
        await _run(url)
    print("\nDone. Paste this whole output back so the checkers can be written from real signals.")


if __name__ == "__main__":
    asyncio.run(main())
