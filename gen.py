
import asyncio
import json
import logging
import re
import signal
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
FEED_FILE = BASE_DIR / "feed.json"
OUTPUT_DIR = BASE_DIR / "screenshot"
MANIFEST_FILE = BASE_DIR / "screenshot-manifest.json"

VIEWPORT = {"width": 1920, "height": 1080}

# Maximum number of websites captured simultaneously
CONCURRENCY = 12

# Wait this many seconds after navigation before taking a screenshot
WAIT_AFTER_LOAD_SECONDS = 2

NAVIGATION_TIMEOUT_MS = 45_000
SCREENSHOT_TIMEOUT_MS = 30_000

# True  = skip if a non-empty screenshot already exists
# False = recapture and overwrite existing screenshots
SKIP_EXISTING = True


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


# ============================================================
# HELPERS
# ============================================================

def safe_filename(value: str) -> str:
    """Convert a string to a filesystem-safe filename component."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value.strip())
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip(" .-")


def make_filename(name: str, url: str) -> str:
    """Build a screenshot filename without the URL scheme."""
    parsed = urlsplit(url.strip())
    url_part = (parsed.netloc + parsed.path).rstrip("/")

    return (
        f"{safe_filename(name) or 'unnamed'}-"
        f"{safe_filename(url_part) or 'website'}.png"
    )


def write_manifest(results: list) -> None:
    """Atomically update the manifest to reduce partial-write risk."""
    temp_path = MANIFEST_FILE.with_suffix(".json.tmp")

    temp_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temp_path.replace(MANIFEST_FILE)


def set_result(results: list, index: int, result: dict) -> None:
    results[index - 1] = result
    write_manifest(results)


# ============================================================
# SCREENSHOT WORKER
# ============================================================

async def capture(
    browser,
    semaphore: asyncio.Semaphore,
    item: dict,
    index: int,
    total: int,
    filename: str,
    results: list,
    lock: asyncio.Lock,
    skip_existing: bool = True,
) -> None:
    url = str(item.get("url", "")).strip()
    output_path = OUTPUT_DIR / filename

    result = {
        "index": index,
        "filename": filename,
        "status": "pending",
        "error": None,
    }

    # Validate URL before attempting navigation.
    parsed = urlsplit(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        result.update(status="failed", error="Invalid URL")

        async with lock:
            set_result(results, index, result)

        logging.warning("[%s/%s] Invalid URL: %r", index, total, url)
        return

    # Skip existing non-empty screenshots without opening a browser.
    if (
        skip_existing
        and output_path.is_file()
        and output_path.stat().st_size > 0
    ):
        result["status"] = "skipped"

        async with lock:
            set_result(results, index, result)

        logging.info(
            "[%s/%s] SKIP: %s already exists",
            index, total, filename,
        )
        return

    async with semaphore:
        context = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
        )

        try:
            page = await context.new_page()

            logging.info(
                "[%s/%s] Opening %s",
                index, total, url,
            )

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                # Attempt to capture even if navigation times out.
                logging.warning(
                    "[%s/%s] Navigation timeout; continuing",
                    index, total,
                )

            # Allow client-side rendering to settle.
            await page.wait_for_timeout(
                WAIT_AFTER_LOAD_SECONDS * 1000
            )

            await page.screenshot(
                path=str(output_path),
                type="png",
                full_page=False,
                animations="disabled",
                timeout=SCREENSHOT_TIMEOUT_MS,
            )

            result.update(status="success", error=None)

            logging.info(
                "[%s/%s] Saved screenshot/%s",
                index, total, filename,
            )

        except asyncio.CancelledError:
            result.update(
                status="interrupted",
                error="Task cancelled",
            )

            logging.warning(
                "[%s/%s] Capture interrupted: %s",
                index, total, url,
            )

            raise

        except Exception as exc:
            result.update(
                status="failed",
                error=str(exc)[:500],
            )

            logging.exception(
                "[%s/%s] Failed: %s",
                index, total, url,
            )

        finally:
            try:
                await context.close()
            finally:
                async with lock:
                    set_result(results, index, result)


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    if not FEED_FILE.is_file():
        raise FileNotFoundError(f"Feed file not found: {FEED_FILE}")

    with FEED_FILE.open("r", encoding="utf-8") as file:
        feed = json.load(file)

    if not isinstance(feed, list):
        raise ValueError("feed.json must contain a JSON array")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Generate unique filenames to avoid overwriting duplicate entries.
    filenames = []
    used_names = set()

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

        while candidate.casefold() in used_names:
            candidate = f"{stem}-{suffix}.png"
            suffix += 1

        used_names.add(candidate.casefold())
        filenames.append(candidate)

    total = len(feed)

    results = [
        {
            "index": index,
            "filename": filenames[index - 1],
            "status": "pending",
            "error": None,
        }
        for index in range(1, total + 1)
    ]

    write_manifest(results)

    logging.info("Total websites: %s", total)
    logging.info("Concurrency: %s", CONCURRENCY)
    logging.info("Skip existing: %s", SKIP_EXISTING)
    logging.info("Wait after navigation: %s seconds", WAIT_AFTER_LOAD_SECONDS)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    semaphore = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        tasks = [
            asyncio.create_task(
                capture(
                    browser=browser,
                    semaphore=semaphore,
                    item=item if isinstance(item, dict) else {},
                    index=index,
                    total=total,
                    filename=filenames[index - 1],
                    results=results,
                    lock=lock,
                    skip_existing=SKIP_EXISTING,
                )
            )
            for index, item in enumerate(feed, start=1)
        ]

        stop_waiter = asyncio.create_task(stop_event.wait())
        all_tasks = asyncio.gather(*tasks, return_exceptions=True)

        try:
            done, _ = await asyncio.wait(
                [all_tasks, stop_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_waiter in done and stop_event.is_set():
                logging.warning(
                    "Stop requested; cancelling unfinished captures"
                )

                for task in tasks:
                    if not task.done():
                        task.cancel()

            await all_tasks

        finally:
            stop_waiter.cancel()

            try:
                await browser.close()
            finally:
                write_manifest(results)

    succeeded = sum(r["status"] == "success" for r in results)
    skipped = sum(r["status"] == "skipped" for r in results)
    failed = sum(r["status"] == "failed" for r in results)
    interrupted = sum(r["status"] == "interrupted" for r in results)
    pending = sum(r["status"] == "pending" for r in results)

    logging.info("=" * 50)
    logging.info("Total:       %s", total)
    logging.info("Succeeded:   %s", succeeded)
    logging.info("Skipped:     %s", skipped)
    logging.info("Failed:      %s", failed)
    logging.info("Interrupted: %s", interrupted)
    logging.info("Pending:     %s", pending)
    logging.info("Manifest:    %s", MANIFEST_FILE)
    logging.info("=" * 50)

    if stop_event.is_set():
        raise SystemExit(130)

    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.warning("Interrupted by keyboard")
        raise SystemExit(130)
