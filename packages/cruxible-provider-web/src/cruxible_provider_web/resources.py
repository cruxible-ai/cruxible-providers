"""Local resource checks; installing Python dependencies never installs a browser."""

from pathlib import Path


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as runtime:
        return Path(runtime.chromium.executable_path).is_file()
