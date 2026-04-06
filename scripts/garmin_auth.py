"""
scripts/garmin_auth.py — One-time Garmin Connect authentication via browser.

Opens a Chromium browser window so you can log in to Garmin Connect manually,
bypassing Cloudflare's bot detection that blocks automated logins. Saves the
resulting session cookies to ~/.spin-sync-garmin-session.json for use by
sync.py.

Run this once whenever your session expires (typically weeks to months):
    pip install playwright
    playwright install chromium
    python scripts/garmin_auth.py

The session file path can be overridden with the GARMIN_SESSION_FILE env var.
"""

import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

SESSION_FILE = Path(
    os.environ.get("GARMIN_SESSION_FILE", Path.home() / ".spin-sync-garmin-session.json")
)

GARMIN_SIGNIN = "https://connect.garmin.com/signin"

# URL patterns that indicate a successful login (post-auth redirect destinations)
POST_LOGIN_PATTERNS = ["/modern", "/home", "/dashboard", "/activities"]


def _is_post_login_url(url: str) -> bool:
    return any(p in url for p in POST_LOGIN_PATTERNS)


async def main() -> None:
    print("Launching browser for Garmin Connect login …")
    print(f"Session will be saved to: {SESSION_FILE}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(GARMIN_SIGNIN)

        print("Please log in to Garmin Connect in the browser window.")
        print("This script will continue automatically once you reach the dashboard.\n")

        # Wait until the page navigates to a post-login URL (up to 3 minutes)
        try:
            await page.wait_for_url(
                _is_post_login_url,
                timeout=180_000,
            )
        except Exception:
            print("ERROR: Timed out waiting for login (3 min). Please try again.")
            await browser.close()
            return

        # Wait a moment for background API calls / cookie finalisation
        await page.wait_for_timeout(2_000)

        cookies = await context.cookies()
        await browser.close()

    garmin_cookies = [
        c for c in cookies
        if "garmin" in c.get("domain", "").lower()
    ]

    if not garmin_cookies:
        print("ERROR: No Garmin cookies found after login. Did you complete the login?")
        return

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({"cookies": garmin_cookies}, indent=2))
    SESSION_FILE.chmod(0o600)  # Restrict read access — cookies are sensitive

    print(f"Saved {len(garmin_cookies)} Garmin cookies to {SESSION_FILE}")
    print("You can now run sync.py — no credentials needed until the session expires.")


if __name__ == "__main__":
    asyncio.run(main())
