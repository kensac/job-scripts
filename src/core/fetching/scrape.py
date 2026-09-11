"""Browser page fetch: the guaranteed floor under the cheaper tiers.

api/fetching.py tries the ATS resolver and the static fetch first and lands
here when those decline. Returns the text and the URL the browser actually
landed on, which is the point: an expired posting very often redirects to a
healthy-looking careers index.
"""

from __future__ import annotations

import contextlib
import logging
import time

import ftfy
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait

from core.fetching import ats

logger = logging.getLogger(__name__)

BROWSER_PAGE_LOAD_TIMEOUT: float = 15.0
BROWSER_ELEMENT_WAIT_TIMEOUT: float = 10.0
BROWSER_CONTENT_WAIT: float = 15.0


def get_chrome_options() -> Options:
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    chrome_options.add_argument("--enable-javascript")
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--allow-running-insecure-content")
    return chrome_options


def extract_url_content_ex(url: str) -> tuple[str | None, str | None]:
    """Returns (content, final_url). The final URL matters: an expired posting
    very often 302s to a board index or careers page, and the page that lands
    is perfectly healthy-looking, so without knowing we were redirected, the
    text reads as a live job to both a human and the model."""
    ats_result = ats.resolve(url)
    if ats_result.ok and ats_result.text:
        return ats_result.text, url

    driver = None
    try:
        chrome_options = get_chrome_options()
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(BROWSER_PAGE_LOAD_TIMEOUT)

        logger.debug(f"Starting load for: {url}")
        driver.get(url)

        WebDriverWait(driver, BROWSER_ELEMENT_WAIT_TIMEOUT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"  # type: ignore
        )

        time.sleep(BROWSER_CONTENT_WAIT)

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")  # type: ignore

        body_element = driver.find_element(By.TAG_NAME, "body")
        content = ftfy.fix_text(body_element.text).strip()

        final_url = None
        with contextlib.suppress(Exception):
            final_url = driver.current_url

        if content:
            logger.info(f"Extracted {len(content)} chars from {url}")
            return content, final_url
        else:
            logger.warning(f"No content found for {url}")
            return None, final_url

    except Exception as exc:
        logger.debug(f"Extraction failed for {url}: {exc}")
        return None, None
    finally:
        if driver:
            with contextlib.suppress(Exception):
                driver.quit()
