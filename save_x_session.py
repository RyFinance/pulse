import asyncio
import json
import os
import time
from scrapling.fetchers import AsyncStealthySession

LOGIN_TIMEOUT_SECONDS = 300


async def main():
    print("=" * 50)
    print("X Session Saver for Project Sentiment")
    print("=" * 50)
    print("\nThis will open a browser window for you to log in to X.")
    print("Session will be saved to x_session.json")
    print("-" * 50)

    session = AsyncStealthySession(headless=False)
    await session.start()

    # Create a page and navigate to X login
    page = await session.context.new_page()
    await page.goto("https://x.com/login")
    await page.wait_for_timeout(3000)

    print("\n" + "=" * 50)
    print(f"LOG IN TO X NOW. Waiting up to {LOGIN_TIMEOUT_SECONDS // 60} minutes.")
    print("=" * 50)
    print("\n1. Enter your username/email/phone")
    print("2. Enter your password")
    print("3. Complete any 2FA if enabled")
    print("\nThe session saves automatically once you're logged in.")
    print("Do NOT close the browser window!")

    # Poll until X sets both auth cookies (= logged in) instead of a fixed timer
    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    logged_in = False
    while time.time() < deadline:
        names = {c["name"] for c in await session.context.cookies("https://x.com")}
        if {"auth_token", "ct0"} <= names:
            logged_in = True
            await page.wait_for_timeout(3000)  # let the home feed settle cookies
            break
        await asyncio.sleep(3)

    if not logged_in:
        print("\n⚠ Timed out waiting for login — x_session.json NOT updated.")
        await session.close()
        return

    session_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_session.json")
    storage = await session.context.storage_state()
    storage["saved_at"] = time.time()  # for expiry detection
    with open(session_path, "w", encoding="utf-8") as f:
        json.dump(storage, f)
    print("\n✅ Logged in — session saved to x_session.json")

    await session.close()
    print("You can now run: python run.py --force-scrape --no-dashboard")


if __name__ == "__main__":
    asyncio.run(main())
