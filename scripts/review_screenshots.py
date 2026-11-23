"""Takes the review-page screenshots on the sample items (started by review_screenshots.sh).

python scripts/review_screenshots.py http://localhost:8091 docs/screenshots
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright  # type: ignore[import-not-found]  # dev tool, not in the image

SHOTS = (  # file, supplier whose item is shown (None: the list), viewport width, height
    ("review-list.png", None, 1280, 800),
    ("review-detail.png", "Pellingham Machine Works Inc.", 1440, 1000),
    ("review-fraud.png", "Tarnbrook Courier Services LLC", 1440, 1000),
    ("review-phone-detail.png", "Estudio Nubeazul S.L.", 390, 844),
)


def sign_in(page: Page, base: str) -> None:
    page.goto(f"{base}/login")
    page.fill("input[name=email]", "rita@example.test")
    page.fill("input[name=password]", os.environ["SAMPLE_PASSWORD"])
    page.click("form[action='/login'] button")
    page.wait_for_url(f"{base}/items")


def main(base: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for name, supplier, width, height in SHOTS:
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=2)
            sign_in(page, base)
            if supplier:
                page.click(f"a.row:has-text('{supplier}')")
                page.wait_for_load_state("networkidle")
            page.screenshot(path=str(out / name), full_page=True)
            print(out / name)
            page.close()
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
