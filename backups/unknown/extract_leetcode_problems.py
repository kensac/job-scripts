from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import urlparse

import colorlog
import dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

dotenv.load_dotenv()

openai_api_key = os.environ.get("OPENAI_API_KEY")
if not openai_api_key:
    raise ValueError("OPENAI_API_KEY environment variable must be set")

openai_client = AsyncOpenAI(api_key=openai_api_key)

handler = colorlog.StreamHandler(sys.stdout)
handler.setFormatter(
    colorlog.ColoredFormatter(
        "%(asctime)s - %(log_color)s%(levelname)s%(reset)s - %(message)s",
        datefmt=None,
        reset=True,
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    )
)
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("leetcode_extractor")

INPUT_FILE = "google.txt"
OUTPUT_FILE = "leetcode_problems.json"
BROWSER_PAGE_LOAD_TIMEOUT = 20.0
BROWSER_ELEMENT_WAIT_TIMEOUT = 10.0
BROWSER_CONTENT_WAIT = 5.0
OPENAI_TIMEOUT = 120.0
MAX_CONCURRENT_PAGES = 3
MAX_RECURSION_DEPTH = 2


@dataclass
class Problem:
    title: str
    url: Optional[str]
    description: Optional[str]
    source_url: str
    problem_type: str

    def to_dict(self):
        return asdict(self)


class ProblemData(BaseModel):
    title: str = Field(description="Problem name or brief title")
    url: Optional[str] = Field(None, description="Full leetcode.com URL if available")
    description: Optional[str] = Field(None, description="Problem description or details")
    problem_type: str = Field(description="One of: leetcode_link, problem_name, problem_description, problem_variant")


class ExtractedProblems(BaseModel):
    problems: List[ProblemData] = Field(
        description="List of LeetCode problems found"
    )
    additional_links: List[str] = Field(
        description="List of additional leetcode.com URLs mentioned that should be processed"
    )


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
    return chrome_options


def extract_page_content(url: str) -> Optional[str]:
    driver = None
    try:
        chrome_options = get_chrome_options()
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(BROWSER_PAGE_LOAD_TIMEOUT)

        logger.debug(f"Loading: {url}")
        driver.get(url)

        WebDriverWait(driver, BROWSER_ELEMENT_WAIT_TIMEOUT).until(
            lambda d: d.execute_script('return document.readyState') == 'complete'
        )

        import time
        time.sleep(BROWSER_CONTENT_WAIT)

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

        body_element = driver.find_element(By.TAG_NAME, "body")
        content = body_element.text.strip()

        if content:
            logger.info(f"Extracted {len(content)} chars from {url}")
            return content
        else:
            logger.warning(f"No content found for {url}")
            return None

    except Exception as exc:
        logger.error(f"Extraction failed for {url}: {exc}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


async def extract_problems_with_ai(content: str, source_url: str) -> ExtractedProblems:
    try:
        response = await openai_client.beta.chat.completions.parse(
            model="gpt-5.2",
            messages=[
                {
                    "role": "system",
                    "content": """You are analyzing LeetCode interview experience posts and discussions to extract coding problems.

<objective>
Extract all coding problems mentioned in the content, including:
1. Direct LeetCode problem links (leetcode.com/problems/...)
2. Problem descriptions without links
3. Problem titles or names mentioned
4. Any additional LeetCode URLs that should be processed
</objective>

<problem_classification>
Classify each problem as one of:
- "leetcode_link": Direct link to a LeetCode problem
- "problem_name": Problem title/name mentioned (e.g., "Two Sum", "Longest Substring")
- "problem_description": Detailed problem description without a link
- "problem_variant": Variation or modification of a known problem
</problem_classification>

<extraction_guidelines>
1. For direct LeetCode problem links:
   - Extract the full URL
   - Extract the problem title from the link or surrounding text
   - Set problem_type to "leetcode_link"

2. For problem names/titles mentioned:
   - Extract the exact name (e.g., "Binary Tree Maximum Path Sum")
   - Try to infer the LeetCode URL if possible (format: leetcode.com/problems/problem-name/)
   - Set problem_type to "problem_name"

3. For problem descriptions:
   - Extract the full description
   - Try to match to a known LeetCode problem if possible
   - Set problem_type to "problem_description"

4. For problem variants:
   - Extract the description of the variant
   - Note what the base problem is if mentioned
   - Set problem_type to "problem_variant"

5. Additional links to process:
   - Extract any leetcode.com URLs mentioned (discuss/, problems/, etc.)
   - Only include leetcode.com domain links
   - Exclude duplicate links already in the problems list
</extraction_guidelines>

<output_format>
For each problem, provide:
- title: The problem name or brief title (required)
- url: Full leetcode.com URL if available (optional, null if not found)
- description: Problem description or details (optional, null if not available)
- problem_type: One of the classification types above

For additional_links:
- List of full leetcode.com URLs to process recursively
</output_format>

<important>
- Be thorough: extract ALL problems mentioned, even if briefly referenced
- Preserve exact problem names and descriptions
- Don't invent problems that aren't mentioned
- If a problem is mentioned multiple times, only include it once
</important>""",
                },
                {
                    "role": "user",
                    "content": f"""Extract all coding problems from this content:

Source URL: {source_url}

Content:
{content}""",
                },
            ],
            response_format=ExtractedProblems,
            timeout=OPENAI_TIMEOUT,
        )

        if response.choices[0].message.refusal:
            logger.warning(f"AI refused extraction: {response.choices[0].message.refusal}")
            return ExtractedProblems(problems=[], additional_links=[])

        parsed = response.choices[0].message.parsed
        if not parsed:
            logger.warning("AI returned no parsed response")
            return ExtractedProblems(problems=[], additional_links=[])

        logger.info(f"Extracted {len(parsed.problems)} problems and {len(parsed.additional_links)} links from {source_url}")
        return parsed

    except Exception as exc:
        logger.error(f"AI extraction failed for {source_url}: {exc}")
        return ExtractedProblems(problems=[], additional_links=[])


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def is_leetcode_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return "leetcode.com" in parsed.netloc.lower()
    except:
        return False


async def process_url(
    url: str,
    processed_urls: Set[str],
    all_problems: List[Problem],
    depth: int = 0
) -> None:
    normalized_url = normalize_url(url)

    if normalized_url in processed_urls:
        logger.debug(f"Skipping already processed: {normalized_url}")
        return

    if depth >= MAX_RECURSION_DEPTH:
        logger.debug(f"Max recursion depth reached for: {normalized_url}")
        return

    processed_urls.add(normalized_url)
    logger.info(f"Processing (depth {depth}): {normalized_url}")

    content = await asyncio.to_thread(extract_page_content, normalized_url)
    if not content:
        logger.warning(f"No content extracted from: {normalized_url}")
        return

    extracted = await extract_problems_with_ai(content, normalized_url)

    for prob_data in extracted.problems:
        problem = Problem(
            title=prob_data.title,
            url=prob_data.url,
            description=prob_data.description,
            source_url=normalized_url,
            problem_type=prob_data.problem_type
        )
        all_problems.append(problem)
        logger.info(f"Found problem: {problem.title} ({problem.problem_type})")

    if depth < MAX_RECURSION_DEPTH - 1:
        for link in extracted.additional_links:
            if is_leetcode_url(link):
                normalized_link = normalize_url(link)
                if normalized_link not in processed_urls:
                    logger.info(f"Queuing recursive link: {normalized_link}")
                    await process_url(normalized_link, processed_urls, all_problems, depth + 1)


async def process_urls_batch(urls: List[str]) -> List[Problem]:
    processed_urls: Set[str] = set()
    all_problems: List[Problem] = []

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

    async def process_with_semaphore(url: str):
        async with semaphore:
            await process_url(url, processed_urls, all_problems, depth=0)

    await asyncio.gather(*[process_with_semaphore(url) for url in urls])

    return all_problems


def deduplicate_problems(problems: List[Problem]) -> List[Problem]:
    seen_keys: Set[str] = set()
    deduplicated: List[Problem] = []

    for problem in problems:
        if problem.url:
            key = f"url:{normalize_url(problem.url)}"
        else:
            key = f"title:{problem.title.lower().strip()}"

        if key not in seen_keys:
            seen_keys.add(key)
            deduplicated.append(problem)
        else:
            logger.debug(f"Skipping duplicate: {problem.title}")

    return deduplicated


def save_problems(problems: List[Problem], output_file: str) -> None:
    output_data = {
        "total_problems": len(problems),
        "by_type": {},
        "problems": [p.to_dict() for p in problems]
    }

    for problem in problems:
        ptype = problem.problem_type
        output_data["by_type"][ptype] = output_data["by_type"].get(ptype, 0) + 1

    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    logger.info(f"Saved {len(problems)} problems to {output_file}")
    logger.info(f"Breakdown by type: {output_data['by_type']}")


async def main():
    input_path = Path(INPUT_FILE)
    if not input_path.exists():
        logger.error(f"Input file not found: {INPUT_FILE}")
        return

    with open(input_path, "r") as f:
        urls = [line.strip() for line in f if line.strip()]

    logger.info(f"Loaded {len(urls)} URLs from {INPUT_FILE}")

    all_problems = await process_urls_batch(urls)

    logger.info(f"Extracted {len(all_problems)} total problems before deduplication")

    deduplicated = deduplicate_problems(all_problems)

    logger.info(f"After deduplication: {len(deduplicated)} unique problems")

    save_problems(deduplicated, OUTPUT_FILE)

    logger.info("Processing complete!")


if __name__ == "__main__":
    asyncio.run(main())
