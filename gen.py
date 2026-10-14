import asyncio
import io
import json
import logging
import os
import re
import signal
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# CONFIG
# ============================================================

def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    return value.strip().lower() in ("1", "true", "yes", "y", "on")


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

# Controlled by workflow input / env var SKIP_EXISTING (default: true)
# True  = skip if a non-empty screenshot already exists
# False = recapture and overwrite existing screenshots
SKIP_EXISTING = env_bool("SKIP_EXISTING", True)

# Extra passes for entries that ended as failed/skipped
RETRY_ROUNDS = int(os.getenv("RETRY_ROUNDS", "1"))

# All screenshots are stored as WebP
WEBP_QUALITY = 85
EXTENSION = ".webp"


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
        f"{safe_filename(url_part) or 'website'}{EXTENSION}"
    )


def has_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


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


def save_webp(image: Image.Image, dest: Path) -> None:
    """Save a PIL image as WebP atomically (temp file, then rename)."""
    temp_path = dest.with_name(dest.name + ".tmp")

    image.convert("RGB").save(
        temp_path,
        format="WEBP",
        quality=WEBP_QUALITY,
        method=6,
    )

    temp_path.replace(dest)


def save_png_bytes_as_webp(png_bytes: bytes, dest: Path) -> None:
    with Image.open(io.BytesIO(png_bytes)) as image:
        save_webp(image, dest)


def migrate_png_to_webp() -> None:
    """Convert every existing PNG in the output dir to WebP, then delete the PNG."""
    converted = removed = failed = 0

    for png_path in sorted(OUTPUT_DIR.glob("*.png")):
        webp_path = png_path.with_suffix(EXTENSION)

        try:
            if not has_file(webp_path):
                with Image.open(png_path) as image:
                    save_webp(image, webp_path)
                converted += 1

            png_path.unlink()
            removed += 1

        except Exception as exc:
            failed += 1
            logging.warning(
                "PNG migration failed for %s: %s",
                png_path.name, exc,
            )

    if converted or removed or failed:
        logging.info(
            "PNG migration: converted=%s removed=%s failed=%s",
            converted, removed, failed,
        )


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

    # Existing non-empty screenshot: nothing to do, mark as success.
    if skip_existing and has_file(output_path):
        result.update(status="success", error=None)

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

            png_bytes = await page.screenshot(
                type="png",
                full_page=False,
                animations="disabled",
                timeout=SCREENSHOT_TIMEOUT_MS,
            )

            # Convert to WebP off the event loop (CPU-bound).
            await asyncio.to_thread(
                save_png_bytes_as_webp,
                png_bytes,
                output_path,
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


async def run_batch(
    browser,
    indices: list,
    feed: list,
    filenames: list,
    results: list,
    lock: asyncio.Lock,
    semaphore: asyncio.Semaphore,
    stop_event: asyncio.Event,
    skip_existing: bool,
) -> None:
    total = len(feed)

    tasks = [
        asyncio.create_task(
            capture(
                browser=browser,
                semaphore=semaphore,
                item=feed[index - 1] if isinstance(feed[index - 1], dict) else {},
                index=index,
                total=total,
                filename=filenames[index - 1],
                results=results,
                lock=lock,
                skip_existing=skip_existing,
            )
        )
        for index in indices
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


def reconcile_results(results: list) -> None:
    """Any failed/skipped/pending entry whose file exists becomes success."""
    fixed = 0

    for result in results:
        if result["status"] not in ("failed", "skipped", "pending"):
            continue

        if has_file(OUTPUT_DIR / result["filename"]):
            result.update(status="success", error=None)
            fixed += 1

    if fixed:
        logging.info("Reconciled %s entries to success (file exists)", fixed)


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

    # Convert legacy PNG screenshots to WebP and delete the PNGs.
    migrate_png_to_webp()

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
            candidate = f"{stem}-{suffix}{EXTENSION}"
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
    logging.info("Retry rounds: %s", RETRY_ROUNDS)
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

        try:
            # Pass 1: every website (honours SKIP_EXISTING).
            await run_batch(
                browser,
                list(range(1, total + 1)),
                feed, filenames, results, lock, semaphore, stop_event,
                skip_existing=SKIP_EXISTING,
            )

            # Retry passes: failed/skipped entries are forced to recapture.
            for round_number in range(1, RETRY_ROUNDS + 1):
                if stop_event.is_set():
                    break

                retry_indices = [
                    r["index"]
                    for r in results
                    if r["status"] in ("failed", "skipped")
                    and r.get("error") != "Invalid URL"
                ]

                if not retry_indices:
                    break

                logging.info(
                    "Retry round %s/%s: %s websites",
                    round_number, RETRY_ROUNDS, len(retry_indices),
                )

                await run_batch(
                    browser,
                    retry_indices,
                    feed, filenames, results, lock, semaphore, stop_event,
                    skip_existing=False,
                )

        finally:
            try:
                await browser.close()
            finally:
                # File on disk wins: failed/skipped + file exists -> success.
                reconcile_results(results)
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.warning("Interrupted by keyboard")
        raise SystemExit(130)
