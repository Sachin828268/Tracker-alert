"""
url_normalize.py
~~~~~~~~~~~~~~~~
Additive layer that sits ABOVE the per-store checkers (checkers/*.py are NOT
touched by this): extracts a canonical, unique product identifier from a
product URL so that differently-formatted URLs for the SAME product can be
grouped and checked once per background cycle (see bot.run_stock_check_cycle).
Pure functions, no network/DB/I/O.

── Core safety principle ────────────────────────────────────────────────────
A false MERGE (treating two DISTINCT products as one) is dangerous: it would
apply one product's stock result to a different product, producing wrong
alerts. A false SPLIT (failing to recognise two URLs as the same product)
is harmless: it merely forgoes a dedup saving and the rows are checked
individually, exactly as they are today.

Therefore every extractor is CONSERVATIVE: it returns a canonical id ONLY on a
high-confidence, store-specific product-id match, and None otherwise. When it
returns None, product_group_key() falls back to keying on the raw URL string,
so the row is treated as unique and checked on its own — never merged with
anything except a byte-identical URL (which is unambiguously the same product).

── v1 coverage ──────────────────────────────────────────────────────────────
Active id extraction (high confidence): amazon, flipkart, myntra, bigbasket,
zepto, blinkit, jiomart. Everything else (apple, tataneu, instamart,
reliancedigital, oneplus, croma, …) intentionally returns None for now —
their URL id patterns weren't verified to high confidence, so they keep
behaving exactly as before (only exact-duplicate URL strings dedup for them).
Adding a store later is a one-function change here with no impact on checkers.
"""

import logging
import re
from urllib.parse import urlparse, parse_qs, unquote

logger = logging.getLogger(__name__)

# Unit-separator: a byte that never appears in a URL or site name, used to join
# key components so they can't collide (e.g. site "a" + id "bc" vs "ab" + "c").
_SEP = "\x1f"


# ── Per-store canonical-id extractors ────────────────────────────────────────
# Each takes a full URL string and returns the canonical product id (a str) on
# a confident match, or None. They never raise (normalize_url guards anyway).

# Amazon ASIN: 10-char token after /dp/, /gp/product/, or /gp/aw/d/. The
# negative lookahead pins it to exactly the id token (won't grab the first 10
# chars of a longer segment). ASINs are case-insensitive-safe → upper-cased.
_AMAZON_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d)/([A-Za-z0-9]{10})(?![A-Za-z0-9])"
)


# Query params that, on Amazon redirect/click wrappers, carry the REAL product
# URL (url-encoded). Sponsored search results are served as
# /sspa/click?...&url=%2F…%2Fdp%2F<ASIN>%2F… — so the ASIN is inside `url=`,
# not in the wrapper's own path (which is just /sspa/click). We ONLY look at
# these known redirect params, never at arbitrary params (dib=, sp_csd=, … are
# base64 blobs and must not be scanned, or a chance /dp/ substring could yield
# a bogus ASIN and a dangerous false-merge).
_AMAZON_REDIRECT_PARAMS = ("url", "location")


def _amazon(url: str) -> str | None:
    parsed = urlparse(url)

    # 1) Primary: ASIN directly in the path — clean /dp/, descriptive
    #    slug + /dp/, /gp/product/, /gp/aw/d/. Unchanged from before, so every
    #    format that already worked returns here immediately, identically.
    m = _AMAZON_RE.search(parsed.path)
    if m:
        return m.group(1).upper()

    # 2) Sponsored / redirect wrapper (e.g. /sspa/click): the real product URL
    #    is url-encoded inside a redirect param. Decode it and re-run the SAME
    #    anchored regex on it — still anchored to /dp|/gp/product|/gp/aw/d, so
    #    it can only match a genuine embedded product URL.
    qs = parse_qs(parsed.query)
    for param in _AMAZON_REDIRECT_PARAMS:
        inner = qs.get(param, [None])[0]
        if not inner:
            continue
        # parse_qs already decoded once; unquote again defends against the
        # occasional double-encoded wrapper. Idempotent when nothing's left.
        m = _AMAZON_RE.search(unquote(inner))
        if m:
            return m.group(1).upper()

    return None


# Flipkart: the `pid` query parameter is the canonical product id
# (e.g. pid=MOBGXXXXXXXX). The path's itm<...> segment is a listing id and is
# deliberately NOT used. No pid → None (conservative; some share links omit it).
def _flipkart(url: str) -> str | None:
    pid = parse_qs(urlparse(url).query).get("pid", [None])[0]
    if pid and re.fullmatch(r"[A-Za-z0-9]+", pid):
        return pid.upper()
    return None


# Myntra: numeric product id in the ".../<id>/buy" path tail.
_MYNTRA_RE = re.compile(r"/(\d+)/buy(?:/|$)")


def _myntra(url: str) -> str | None:
    m = _MYNTRA_RE.search(urlparse(url).path)
    return m.group(1) if m else None


# BigBasket: numeric id in the "/pd/<id>/..." path segment.
_BIGBASKET_RE = re.compile(r"/pd/(\d+)")


def _bigbasket(url: str) -> str | None:
    m = _BIGBASKET_RE.search(urlparse(url).path)
    return m.group(1) if m else None


# Zepto: the pvid (product-variant id, a UUID) in "/pvid/<uuid>".
_ZEPTO_RE = re.compile(r"/pvid/([0-9a-fA-F][0-9a-fA-F\-]{7,})")


def _zepto(url: str) -> str | None:
    m = _ZEPTO_RE.search(urlparse(url).path)
    return m.group(1).lower() if m else None


# Blinkit: numeric prid in "/prid/<id>".
_BLINKIT_RE = re.compile(r"/prid/(\d+)")


def _blinkit(url: str) -> str | None:
    m = _BLINKIT_RE.search(urlparse(url).path)
    return m.group(1) if m else None


# JioMart: trailing numeric product id at the end of a "/p/..." path
# (e.g. /p/groceries/<slug>/590000123). Require >=6 digits to avoid matching
# short category codes.
_JIOMART_RE = re.compile(r"/p/.+/(\d{6,})/?$")


def _jiomart(url: str) -> str | None:
    m = _JIOMART_RE.search(urlparse(url).path)
    return m.group(1) if m else None


# Registry: site key -> extractor. Sites absent here always fall back to the
# raw-URL key (no id-based merging) — the conservative default.
_EXTRACTORS = {
    "amazon": _amazon,
    "flipkart": _flipkart,
    "myntra": _myntra,
    "bigbasket": _bigbasket,
    "zepto": _zepto,
    "blinkit": _blinkit,
    "jiomart": _jiomart,
}


def normalize_url(site: str, url: str) -> str | None:
    """
    Return the canonical product id for (site, url) on a confident match, else
    None. Never raises — any extractor error degrades to None (→ raw-URL key),
    which is the safe direction (no merge).
    """
    extractor = _EXTRACTORS.get(site)
    if extractor is None:
        return None
    try:
        return extractor(url)
    except Exception as exc:  # pragma: no cover — defensive only
        logger.warning(f"[url_normalize] {site} extractor failed on {url!r}: {exc}")
        return None


def product_group_key(site: str, url: str, pincode: str | None = None) -> str:
    """
    The dedup grouping key for a tracked product row. Rows sharing a key are
    checked ONCE and the result fans out to all of them.

    Key = (site, canonical-id-or-raw-url, pincode). Pincode is part of the key
    on purpose: some stores (Apple most notably) return genuinely different
    stock per pincode, so two users at different pincodes must NEVER share a
    single check — including pincode guarantees that while still deduping the
    common case (same product, same/empty pincode).

    When normalize_url returns None the key uses the raw URL string, so only
    byte-identical URLs (unambiguously the same product) ever merge for that
    store — a conservative, correctness-preserving fallback.
    """
    pin = pincode or ""
    norm = normalize_url(site, url)
    if norm:
        return f"{site}{_SEP}id:{norm}{_SEP}{pin}"
    return f"{site}{_SEP}raw:{url.strip()}{_SEP}{pin}"
