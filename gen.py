import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
FEED_FILE = BASE_DIR / "feed.json"
OUTPUT_DIR = BASE_DIR / "screenshot"
MANIFEST_FILE = BASE_DIR / "screenshot-manifest.json"

VIEWPORT = {"width": 1920, "height": 1080}
CONCURRENCY = 4
WAIT_AFTER_LOAD_SECONDS = 10
NAVIGATION_TIMEOUT_MS = 45_000
SCREENSHOT_TIMEOUT_MS = 30_000


def safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value.strip())
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip(" .-")


def make_filename(name: str, url: str) -> str:
    parsed = urlsplit(url.strip())
    url_part = (parsed.netloc + parsed.path).rstrip("/")
    return (
        f"{safe_filename(name) or 'unnamed'}-"
        f"{safe_filename(url_part) or 'website'}.png"
    )


async def capture(browser, semaphore, item, index, filename):
    url = str(item.get("url", "")).strip()
    result = {
        "index": index,
        "filename": filename,
        "status": "failed",
        "error": None,
    }

    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
        result["error"] = "Invalid URL"
        print(f"[{index}] SKIP: {url!r}")
        return result

    async with semaphore:
        context = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
        )

        try:
            page = await context.new_page()
            print(f"[{index}] Opening: {url}")

            try:
                await page.goto(
                    url,
                    wait_until="load",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                # Capture anyway if the website never finishes loading.
                print(f"[{index}] WARNING: Load timeout; continuing")

            # Wait 10 seconds after navigation/load attempt.
            await page.wait_for_timeout(WAIT_AFTER_LOAD_SECONDS * 1000)

            await page.screenshot(
                path=str(OUTPUT_DIR / filename),
                type="png",
                full_page=False,
                animations="disabled",
                timeout=SCREENSHOT_TIMEOUT_MS,
            )

            result["status"] = "success"
            print(f"[{index}] OK: screenshot/{filename}")

        except Exception as exc:
            result["error"] = str(exc)[:500]
            print(f"[{index}] ERROR: {url}: {exc}")

        finally:
            await context.close()

    return result


async def main():
    if not FEED_FILE.is_file():
        raise FileNotFoundError(f"Missing {FEED_FILE}")

    with FEED_FILE.open(encoding="utf-8") as f:
        feed = json.load(f)

    if not isinstance(feed, list):
        raise ValueError("feed.json must contain a JSON array")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Generate unique filenames even if two entries have the same name and URL.
    filenames = []
    used = set()

    for item in feed:
        if not isinstance(item, dict):
            item = {}

        filename = make_filename(
            str(item.get("name", "")),
            str(item.get("url", "")),
        )

        stem = Path(filename).stem
        candidate = filename
        suffix = 2

        while candidate.lower() in used:
            candidate = f"{stem}-{suffix}.png"
            suffix += 1

        used.add(candidate.lower())
        filenames.append(candidate)

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        try:
            tasks = [
                capture(browser, semaphore, item, index, filenames[index - 1])
                for index, item in enumerate(feed, start=1)
            ]
            results = await asyncio.gather(*tasks)
        finally:
            await browser.close()

    with MANIFEST_FILE.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    succeeded = sum(r["status"] == "success" for r in results)
    failed = len(results) - succeeded

    print(f"\nFinished: {succeeded} succeeded, {failed} failed")
    print(f"Manifest: {MANIFEST_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
