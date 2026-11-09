"""Full-article reading for pages that hide text behind "read more".

Step 1 (in ingest.py) downloads each page normally — fast, and often enough, because
many sites ship the whole article in the page and only hide part of it until a click.

Step 2 (here) runs only when the text still looks cut short. It opens the page in a
real headless browser, clicks every "read more" / "show more" / "continue reading"
control, scrolls to the bottom so lazy-loaded sections appear, and then reads the
article.

What this deliberately does NOT do: get past paywalls. A "subscribe to continue"
wall is the publisher charging for the article. Those pages are detected, never
clicked through, and reported in the email so you know that article was only
partly readable.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Sequence

log = logging.getLogger(__name__)

# Text of controls that expand the article in place.
EXPAND_TEXT = re.compile(
    r"^\s*(?:read|show|view|load|see|continue|expand|open)\s+"
    r"(?:more|full|the full|entire|rest|complete|reading|all)\b"
    r"|^\s*(?:more|expand|continue reading|read full (?:story|article))\s*[›»>+\u2192.]*\s*$",
    re.I)

# Anything that leads to a login, subscription or payment. Never clicked.
PAYWALL_CONTROL = re.compile(
    r"subscri|sign\s?in|log\s?in|register|premium|paywall|unlock|membership|"
    r"buy|pay\b|trial|upgrade|plus\b|prime\b", re.I)

# Text that means the article sits behind a paywall.
PAYWALL_TEXT = re.compile(
    r"subscribe to (?:continue|read|unlock)|this (?:is a )?(?:premium|subscriber[- ]only|"
    r"exclusive) (?:article|story|content)|to continue reading,? (?:please )?"
    r"(?:subscribe|sign in|log in)|already a subscriber|unlock this (?:article|story)|"
    r"become a (?:member|subscriber) to read", re.I)

# Signs the downloaded text stops before the article ends.
CUT_SHORT_END = re.compile(
    r"(?:read more|show more|continue reading|view more|load more|read full "
    r"(?:story|article)|\.\.\.|\u2026)\s*$", re.I)

MIN_FULL_ARTICLE = 1500     # characters; real funding stories are almost always longer


def is_paywalled(text: str) -> bool:
    return bool(PAYWALL_TEXT.search(text or ""))


def looks_cut_short(text: str, html: str = "") -> bool:
    """True when the article text probably stops before the end of the article."""
    t = (text or "").strip()
    if len(t) < MIN_FULL_ARTICLE:
        return True
    if CUT_SHORT_END.search(t[-200:]):
        return True
    # An expand control present in the page means some text may still be hidden.
    if html and re.search(r">\s*(?:read|show|view|load)\s+more\s*<", html, re.I):
        return True
    return False


def is_expand_control(label: str) -> bool:
    label = (label or "").strip()
    if not label or len(label) > 40:
        return False
    return bool(EXPAND_TEXT.search(label)) and not PAYWALL_CONTROL.search(label)


_CLICK_SCRIPT = """
(args) => {
  const [expandSrc, payFlags] = args;
  const expand = new RegExp(expandSrc, 'i');
  const pay = new RegExp(payFlags, 'i');
  const here = location.host;
  let clicked = 0;
  const els = document.querySelectorAll(
    'button, a, span, div, p, [role="button"], summary');
  for (const el of els) {
    const label = (el.innerText || el.textContent || '').trim();
    if (!label || label.length > 40) continue;
    if (!expand.test(label) || pay.test(label)) continue;
    if (el.offsetParent === null) continue;               // not visible
    if (el.tagName === 'A' && el.href) {
      try {
        const u = new URL(el.href, location.href);
        if (u.host !== here) continue;                    // never leave the site
        if (pay.test(u.pathname)) continue;               // never go to a pay page
        if (u.pathname !== location.pathname && !u.hash) continue;  // stay on the article
      } catch (e) { continue; }
    }
    try { el.click(); clicked++; } catch (e) {}
  }
  return clicked;
}
"""


def fetch_full_pages(urls: Sequence[str], extract: Callable[[str], str],
                     max_pages: int = 30, timeout_ms: int = 20000) -> dict[str, dict]:
    """Open each URL in a headless browser, expand it, and return the article text.

    Returns {url: {"text": str, "clicked": int, "paywalled": bool}}. A page that fails
    to load is simply absent from the result; the caller keeps what it already had.
    """
    results: dict[str, dict] = {}
    if not urls:
        return results
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("browser not installed — 'read more' pages read from plain download only")
        return results

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            java_script_enabled=True)
        # Skip images, video and fonts: faster, and not needed to read text.
        ctx.route("**/*", lambda r: r.abort()
                  if r.request.resource_type in ("image", "media", "font") else r.continue_())
        page = ctx.new_page()
        for url in list(urls)[:max_pages]:
            try:
                try:
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                except Exception as nav_exc:                # noqa: BLE001
                    # Google News forwards to the publisher while its own page is
                    # still opening, which aborts the first page load. That is the
                    # forward working, not a failure — carry on and follow it.
                    if not re.search(r"interrupted by another navigation|ERR_ABORTED",
                                     str(nav_exc)):
                        raise
                # Google News links forward to the publisher with JavaScript a moment
                # after loading. Wait for that hop before touching the page, or the
                # page is torn down mid-read.
                if "news.google." in page.url:
                    try:
                        page.wait_for_url(lambda u: "news.google." not in u,
                                          timeout=timeout_ms)
                    except Exception:                       # noqa: BLE001
                        pass
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                except Exception:                           # noqa: BLE001
                    pass
                page.wait_for_timeout(1500)
                # Still parked on a Google page: follow its link to the publisher.
                if "google." in page.url:
                    try:
                        href = page.evaluate("""() => {
                          const a=[...document.querySelectorAll('a[href^="http"]')]
                            .map(x=>x.href).find(h=>!/google\\./.test(h));
                          return a||null; }""")
                        if href:
                            page.goto(href, timeout=timeout_ms, wait_until="domcontentloaded")
                            page.wait_for_timeout(1500)
                    except Exception:                       # noqa: BLE001
                        pass
                clicked = 0
                for _ in range(3):                          # some articles expand in stages
                    try:
                        n = page.evaluate(_CLICK_SCRIPT,
                                          [EXPAND_TEXT.pattern, PAYWALL_CONTROL.pattern])
                    except Exception:                       # noqa: BLE001
                        # The page moved on (a late redirect). Let it settle; read it.
                        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                        n = 0
                    clicked += n
                    page.mouse.wheel(0, 20000)              # trigger lazy-loaded sections
                    page.wait_for_timeout(1200)
                    if not n:
                        break
                html = page.content()
                text = extract(html)
                results[url] = {"text": text, "clicked": clicked,
                                "paywalled": is_paywalled(text)}
            except Exception as exc:                        # noqa: BLE001
                log.warning("browser read failed %s: %s", url, exc)
        browser.close()
    return results
