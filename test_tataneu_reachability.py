"""
One-off diagnostic: is TataNeu reachable and does its product page carry
parseable stock/availability data (JSON-LD, button state, OOS text) — same
questions asked of Croma and Flipkart before committing to building a real
checker for a new site.

Not part of the app — bot.py never imports this. Tests 3 ways to fetch the
same product page and compares them:
  A) Direct (no proxy) from wherever this runs — tests whether Railway's own
     IP is blocked outright, same question asked of Croma.
  B) Via Scrape.do, render=false — cheapest mode (1 credit), works if
     TataNeu's stock data is present without JS execution.
  C) Via Scrape.do, render=true — full headless render (5 credits), needed
     only if (B) doesn't carry the real data (JS-injected content).

Usage (run via `railway run python3 test_tataneu_reachability.py`, wherever
SCRAPEDO_KEY is a real credential):

    python3 test_tataneu_reachability.py [product_url]

If no URL is given, defaults to the Sony Astro Bot product page.
"""

import asyncio
import json
import sys

import httpx
from bs4 import BeautifulSoup

from checkers.common import build_scraper_url

DEFAULT_URL = (
    "https://www.tataneu.com/commerce/product-details"
    "?skuId=319631&skuName=sony-astro-bot-for-ps5-third-person-shooter-ppsa-21567-&showPDPWidgets=true"
)

_ADD_PATTERNS = ("add to cart", "add to bag", "buy now", "add item")
_OOS_PATTERNS = ("out of stock", "sold out", "currently unavailable", "notify me when available", "notify me")


async def _fetch_direct(url: str) -> tuple[int, str] | None:
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-IN,en;q=0.9",
            })
            return resp.status_code, resp.text
    except Exception as exc:
        print(f"  Direct fetch failed: {type(exc).__name__}: {exc}")
        return None


async def _fetch_via_scrapedo(url: str, render_js: bool) -> tuple[int, str] | None:
    scraper_url = build_scraper_url(url, render_js=render_js)
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(scraper_url)
            return resp.status_code, resp.text
    except Exception as exc:
        print(f"  Scrape.do fetch failed: {type(exc).__name__}: {exc}")
        return None


def _analyze(label: str, status: int, html: str) -> None:
    print(f"\n=== {label} ===")
    print(f"  HTTP status: {status}")
    print(f"  HTML byte size: {len(html.encode('utf-8')):,}")

    if status != 200:
        print("  (non-200 — skipping content analysis)")
        return

    html_lower = html.lower()
    soup = BeautifulSoup(html, "html.parser")

    ld_availability = None
    ld_count = 0
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("offers"):
                ld_count += 1
                offers = item.get("offers", {})
                avail = offers.get("availability", "") if isinstance(offers, dict) else ""
                if avail:
                    ld_availability = avail
    print(f"  JSON-LD blocks with 'offers': {ld_count}, availability={ld_availability!r}")

    add_hits = [p for p in _ADD_PATTERNS if p in html_lower]
    oos_hits = [p for p in _OOS_PATTERNS if p in html_lower]
    print(f"  Add-to-cart-like text present: {add_hits or 'none'}")
    print(f"  OOS-like text present: {oos_hits or 'none'}")

    btn_count = 0
    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True).lower()
        if text in ("add", "+") or any(p in text for p in _ADD_PATTERNS):
            print(
                f"  cart <button> text={btn.get_text(strip=True)[:40]!r} "
                f"class={btn.get('class')} disabled_attr={btn.get('disabled')!r} "
                f"aria-disabled={btn.get('aria-disabled')!r}"
            )
            btn_count += 1
            if btn_count >= 5:
                break
    if btn_count == 0:
        print("  no cart/add button matched (may be JS-injected — check render=true result)")

    print(f"  ₹ price symbol present: {'₹' in html}")
    print(f"  head snippet: {html[:200]!r}")


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"Testing: {url}\n")

    print("--- A) Direct fetch (no proxy) ---")
    direct = await _fetch_direct(url)
    if direct:
        _analyze("Direct (no proxy)", *direct)

    print("\n--- B) Via Scrape.do, render=false (1 credit) ---")
    no_js = await _fetch_via_scrapedo(url, render_js=False)
    if no_js:
        _analyze("Scrape.do render=false", *no_js)

    print("\n--- C) Via Scrape.do, render=true (5 credits) ---")
    with_js = await _fetch_via_scrapedo(url, render_js=True)
    if with_js:
        _analyze("Scrape.do render=true", *with_js)

    print(
        "\nCompare the three: if (A) succeeds, Railway's IP isn't blocked at all (unlike "
        "Croma). If (B) already shows JSON-LD availability or a real add-to-cart button, "
        "render=true isn't needed (cheaper, like Flipkart). If only (C) shows real content, "
        "TataNeu needs JS rendering (like Croma/BigBasket/Zepto/Blinkit). If ALL THREE fail "
        "or show no usable signal, TataNeu isn't a viable Croma replacement either."
    )


if __name__ == "__main__":
    asyncio.run(main())
