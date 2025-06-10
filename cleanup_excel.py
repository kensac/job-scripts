from __future__ import annotations

import argparse
import csv
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure module-level logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Constants
HEAD_TIMEOUT = 5  # seconds
GET_TIMEOUT = 10  # seconds
KEYWORD_LIST: List[str] = [
    "job not found",
    "position has been filled",
    "no longer accepting applications",
    "job expired",
    "sorry, this job has expired",
    "the page you are looking for doesn't exist.",
]

@dataclass(frozen=True)
class URLCheckResult:
    url: str
    status: Optional[int]
    expired: bool
    reason: str

class JobURLChecker:
    """
    Checks job URLs for expiration or error conditions.
    """

    def __init__(
        self,
        verify_content: bool = False,
        retries: int = 0,
        max_workers: int = 10,
        delay: float = 0.0,
    ) -> None:
        self.verify_content = verify_content
        self.retries = retries
        self.max_workers = max_workers
        self.delay = delay
        self.session: requests.Session = requests.Session()

        if retries > 0:
            retry_strategy = Retry(
                total=retries,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["HEAD", "GET"],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)

    def check_url(self, url: str) -> URLCheckResult:
        url = url.strip()
        if not url:
            return URLCheckResult(url=url, status=None, expired=False, reason="Empty URL")

        # Attempt HEAD request first
        try:
            head_resp = self.session.head(url, timeout=HEAD_TIMEOUT)
            status = head_resp.status_code
            if status == 404:
                return URLCheckResult(url, status, True, "404 Not Found (HEAD)")
            if 400 <= status < 600:
                return URLCheckResult(url, status, True, f"HTTP {status} (HEAD)")
        except requests.RequestException:
            logger.debug("HEAD request failed for %s", url)
            status = None

        # Fallback to GET request
        try:
            get_resp = self.session.get(url, timeout=GET_TIMEOUT)
            status = get_resp.status_code

            if status == 429:
                retry_after = get_resp.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 60
                logger.warning("429 for %s, sleeping %ds", url, wait)
                time.sleep(wait)

            if status == 404:
                return URLCheckResult(url, status, True, "404 Not Found (GET)")
            if 400 <= status < 600 and status != 429:
                return URLCheckResult(url, status, True, f"HTTP {status} (GET)")

            # Content-based checks
            if self.verify_content:
                text_lower = get_resp.text.lower()
                for kw in KEYWORD_LIST:
                    if kw in text_lower:
                        return URLCheckResult(
                            url, status, True, f"Content indicates expired: '{kw}'"
                        )

                lower_url = url.lower()
                # Workday-specific logic
                if 'workday' in lower_url:
                    soup = BeautifulSoup(get_resp.text, 'html.parser')
                    meta = soup.find('meta', {'property': 'og:description'})
                    if not meta or not meta.get('content', '').strip():
                        return URLCheckResult(
                            url, status, True, "Workday page missing og:description"
                        )
                # Greenhouse-specific logic
                if 'greenhouse' in lower_url and '?error=true' in get_resp.url:
                    return URLCheckResult(url, status, True, "Greenhouse page indicates expired job")
                # Jobvite-specific logic
                if 'jobvite' in lower_url and '?error=404' in get_resp.url:
                    return URLCheckResult(url, status, True, "Jobvite page indicates expired job")

            return URLCheckResult(url, status, False, "OK")

        except requests.ReadTimeout:
            return URLCheckResult(url, status, False, "Read Timeout")
        except requests.RequestException as exc:
            # Network or other errors: uncertain, not expired
            return URLCheckResult(url, status, False, f"Request Error: {exc}")

    def _single_task(self, url: str) -> URLCheckResult:
        attempt = 0
        last: Optional[URLCheckResult] = None
        while attempt <= self.retries:
            if self.delay:
                time.sleep(self.delay)
            result = self.check_url(url)
            if result.status is not None:
                return result
            last = result
            attempt += 1
        # Max retries but uncertain if expired
        return last or URLCheckResult(url, None, False, "Max retries exceeded")

    def process_urls(self, urls: List[str]) -> List[URLCheckResult]:
        total = len(urls)
        results: List[URLCheckResult] = [URLCheckResult(u, None, False, "Not processed") for u in urls]
        start = time.time()

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_idx = {executor.submit(self._single_task, u): i for i, u in enumerate(urls)}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    u = urls[idx]
                    logger.error("Error processing %s: %s", u, exc)
                    # Uncertain error: not marking expired
                    results[idx] = URLCheckResult(u, None, False, f"Exception: {exc}")

        elapsed = time.time() - start
        logger.info("Processed %d URLs in %.2fs", total, elapsed)
        return results

    def save_to_csv(self, results: List[URLCheckResult], path: Path) -> None:
        """Writes URLCheckResults to CSV, preserving sheet text column."""
        with path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["URL", "Status", "Expired", "Reason", "Sheet Text"])
            for r in results:
                sheet_text = "No Longer Interested" if r.expired else ""
                writer.writerow([r.url, r.status or '', r.expired, r.reason, sheet_text])
        logger.info("Results saved to %s", path)

    def run(self, urls: List[str], output: Optional[Path] = None) -> List[URLCheckResult]:
        results = self.process_urls(urls)
        if output:
            self.save_to_csv(results, output)
        # also write out the sheet text column to a seperate file
        sheet_text_path = output.with_suffix('.sheet_text.csv')
        with sheet_text_path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            for r in results:
                sheet_text = "No Longer Interested" if r.expired else " "
                writer.writerow([sheet_text])
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Check job URLs for expiration.")
    parser.add_argument(
        '--input', '-i', type=Path, default=Path('urls.txt'),
        help='Path to file with one URL per line'
    )
    parser.add_argument(
        '--output', '-o', type=Path, default=Path('output.csv'),
        help='CSV file to write results'
    )
    args = parser.parse_args()

    if not args.input.exists():
        logger.error("Input file not found: %s", args.input)
        return

    urls = [line.strip() for line in args.input.read_text(encoding='utf-8').splitlines() if line.strip()]
    checker = JobURLChecker(verify_content=True, retries=1, max_workers=5, delay=0.5)
    checker.run(urls, args.output)

if __name__ == '__main__':
    main()
