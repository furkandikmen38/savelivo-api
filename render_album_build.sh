#!/usr/bin/env bash
set -euo pipefail

# Optional album test build; do not alter the standard deployment.
python -m pip install -r requirements-album.txt
export PLAYWRIGHT_BROWSERS_PATH=0
python -m playwright install --only-shell chromium
python -m py_compile instagram_album_browser.py instagram_photos.py media_jobs.py
python -m unittest -v test_album_route
# Abort the build if the installed browser cannot start in this environment.
# No outbound Instagram request, no credentials and no provider traffic.
python - <<'PY'
from playwright.sync_api import sync_playwright
from instagram_album_browser import asset_key, _allowed_request
assert asset_key("https://scontent.cdninstagram.com/a.jpg?size=1") == "/a.jpg"
assert not asset_key("https://cdninstagram.com.evil.invalid/a.jpg")
assert not _allowed_request("http://www.instagram.com/")
assert not _allowed_request("https://127.0.0.1/")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, timeout=15000)
    try:
        page = browser.new_page()
        page.set_content("<main>SaveLivo browser smoke test</main>", timeout=5000)
        assert page.locator("main").inner_text() == "SaveLivo browser smoke test"
    finally:
        browser.close()
print("SAVELIVO_ALBUM_BROWSER_READY")
PY
