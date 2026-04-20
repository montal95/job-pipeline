"""
Liveness checker — decides whether a job posting is still accepting applications.

Two-stage design:
  _classify_liveness() is a pure function over collected signals (http status,
  final URL, page body, apply controls). Easy to unit-test.

  check_job_liveness() drives a Playwright session to gather those signals,
  then hands off to the classifier. Returns (result, reason) so the CLI can
  surface a human-readable explanation alongside the classification.

Priority (see PRD F4-T3):
  1. 404 / 410 → expired
  2. Expired phrase in page body → expired
  3. Redirect away from the job URL path → expired
  4. Apply controls present in DOM → active
  5. Default → uncertain
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from rich.console import Console

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None  # type: ignore[assignment]

console = Console()

LivenessResult = Literal["active", "expired", "uncertain"]

EXPIRED_PHRASES: list[str] = [
    "no longer accepting applications",
    "this position is no longer available",
    "this job is no longer available",
    "position has been filled",
    "this role is closed",
    "application deadline has passed",
    "we are no longer accepting",
    "job posting has expired",
    "this posting has expired",
]

APPLY_SELECTORS: list[str] = [
    "button:has-text('Apply')",
    "a:has-text('Apply for this job')",
    "a:has-text('Apply now')",
    "[data-testid*='apply']",
    "#apply-button",
]


def _final_url_lost_job_path(original_url: str, final_url: str) -> bool:
    """
    Heuristic: if the original URL path had a job-specific slug/ID and the
    final URL doesn't contain that slug, treat it as a redirect-to-generic
    (e.g. /jobs/abc-123 → /careers).
    """
    if not original_url or not final_url:
        return False
    orig_path = urlparse(original_url).path.strip("/")
    final_path = urlparse(final_url).path.strip("/")
    if not orig_path:
        return False
    orig_segments = [s for s in orig_path.split("/") if s]
    # A bare /careers or /jobs landing page doesn't look job-specific
    if len(orig_segments) < 2:
        return False
    last_segment = orig_segments[-1]
    return last_segment not in final_path


def _classify_liveness(
    http_status: int,
    final_url: str,
    body_text: str,
    apply_controls: list[str],
    original_url: str = "",
) -> LivenessResult:
    if http_status in (404, 410):
        return "expired"

    lowered = (body_text or "").lower()
    if any(phrase in lowered for phrase in EXPIRED_PHRASES):
        return "expired"

    if _final_url_lost_job_path(original_url, final_url):
        return "expired"

    if apply_controls:
        return "active"

    return "uncertain"


async def check_job_liveness(url: str) -> tuple[LivenessResult, str]:
    """
    Visit `url` with Playwright, collect liveness signals, classify.
    On any Playwright error returns ("uncertain", f"error: {exc}") per PRD
    scraper rule — CLI never crashes on a dead URL.
    """
    if async_playwright is None:
        return ("uncertain", "error: Playwright not available on this platform")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            http_status = response.status if response else 0
            final_url = page.url
            body_text = await page.inner_text("body")
            apply_controls: list[str] = []
            for selector in APPLY_SELECTORS:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        text = (await el.inner_text()).strip()
                        if text:
                            apply_controls.append(text)
                except Exception:
                    continue
            await browser.close()
    except Exception as exc:
        return ("uncertain", f"error: {exc}")

    result = _classify_liveness(
        http_status=http_status,
        final_url=final_url,
        body_text=body_text,
        apply_controls=apply_controls,
        original_url=url,
    )
    reason = (
        f"http={http_status} final={final_url} "
        f"apply_controls={len(apply_controls)}"
    )
    return (result, reason)
