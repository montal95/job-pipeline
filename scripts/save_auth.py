"""
save_auth.py — Interactive Playwright login helper for Phase 2.

Launches a headed Chrome window with one tab per platform, waits for you
to complete all logins, then saves each session to playwright/.auth/.

Usage (from repo root, Windows only):
    python scripts/save_auth.py --platform linkedin
    python scripts/save_auth.py --platform ziprecruiter
    python scripts/save_auth.py --platform all
    python scripts/save_auth.py --platform all --dry-run

Security:
    playwright/.auth/ is in .gitignore. These files contain session cookies
    equivalent to logged-in credentials — never commit them.

Requirements:
    pip install playwright && playwright install chrome
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

AUTH_DIR = Path(__file__).parent.parent / "playwright" / ".auth"

PLATFORM_CONFIG: dict[str, dict] = {
    "linkedin": {
        "login_url": "https://www.linkedin.com/login",
        "auth_file": "linkedin.json",
        "ready_hint": "Log in to LinkedIn, then wait until you can see your feed.",
    },
    "ziprecruiter": {
        "login_url": "https://www.ziprecruiter.com/login",
        "auth_file": "ziprecruiter.json",
        "ready_hint": "Log in to ZipRecruiter, then wait until you can see job matches.",
    },
}

def _dry_run(platforms: list[str]) -> None:
    """Validate Chrome availability and auth dir writeability without opening a browser."""
    print("--- Dry run ---")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            browser.close()
        print("✓ Chrome is available via Playwright")
    except Exception as exc:
        print(f"✗ Chrome launch failed: {exc}")
        print("  Run: playwright install chrome")
        sys.exit(1)

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    test_file = AUTH_DIR / ".write_test"
    try:
        test_file.write_text("ok")
        test_file.unlink()
        print(f"✓ Auth directory is writable: {AUTH_DIR}")
    except Exception as exc:
        print(f"✗ Auth directory not writable: {exc}")
        sys.exit(1)

    for platform in platforms:
        cfg = PLATFORM_CONFIG[platform]
        auth_path = AUTH_DIR / cfg["auth_file"]
        status = "exists" if auth_path.exists() else "not yet saved"
        print(f"  {platform}: {auth_path} ({status})")

    print("\nDry run passed. Remove --dry-run to open the browser and save sessions.")

async def save_sessions(platforms: list[str]) -> None:
    """
    Open one Chrome window with one tab per platform.
    Wait for the user to log in to all tabs, then save each session.
    """
    from playwright.async_api import async_playwright

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("  Job Pipeline — Auth Session Setup")
    print("=" * 60)
    print(f"\n  Platforms: {', '.join(platforms)}")
    print(f"  Sessions will be saved to: {AUTH_DIR}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome")
        context = await browser.new_context()

        # Open one tab per platform. The first tab is reused; extras are created.
        pages = []
        for i, platform in enumerate(platforms):
            cfg = PLATFORM_CONFIG[platform]
            if i == 0:
                page = await context.new_page()
            else:
                page = await context.new_page()
            await page.goto(cfg["login_url"])
            pages.append((platform, page, cfg))
            print(f"  [{i+1}] {platform.upper()} tab opened → {cfg['login_url']}")

        print()
        for platform, _, cfg in pages:
            print(f"  {platform.upper()}: {cfg['ready_hint']}")

        print()
        input("  Press Enter here once you are fully logged in to ALL tabs: ")
        print()

        # Save each tab's session state independently
        for platform, page, cfg in pages:
            auth_path = AUTH_DIR / cfg["auth_file"]
            try:
                await context.storage_state(path=str(auth_path))
                print(f"  ✓ {platform}: session saved → {auth_path}")
                print(f"    (Sessions typically last ~30 days. Re-run this script when expired.)")
            except Exception as exc:
                print(f"  ✗ {platform}: failed to save session — {exc}")

        await browser.close()

    print("\nDone. You can now run: pipeline discover --sources linkedin,ziprecruiter")

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Save Playwright auth sessions for LinkedIn and ZipRecruiter."
    )
    parser.add_argument(
        "--platform",
        choices=["linkedin", "ziprecruiter", "all"],
        required=True,
        help="Which platform(s) to authenticate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate Chrome availability and auth dir writeability without opening a browser.",
    )
    args = parser.parse_args()

    platforms = list(PLATFORM_CONFIG.keys()) if args.platform == "all" else [args.platform]

    if args.dry_run:
        _dry_run(platforms)
        return

    if sys.platform != "win32":
        print("⚠ Warning: Playwright Chrome is only supported on Windows in this project.")
        print("  The browser may not launch correctly on this platform.")
        print("  Use --dry-run to validate the environment first.\n")

    asyncio.run(save_sessions(platforms))


if __name__ == "__main__":
    main()
