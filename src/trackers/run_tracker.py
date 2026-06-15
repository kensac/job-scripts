#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Dict, List

import colorlog
from core import pittcsc_simplify
from core.configs import load_configs, load_groups
from core.pittcsc_simplify import RunSummary

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
logger = logging.getLogger(__name__)

CONFIGS: Dict[str, Dict[str, str]] = load_configs()


def run_tracker(config_name: str, retry_failed: bool = False, reevaluate_custom: bool = False, apply_filter: str | None = None) -> tuple[int, RunSummary | None]:
    if config_name not in CONFIGS:
        logger.error(f"Unknown configuration: {config_name}")
        logger.info(f"Available configurations: {', '.join(CONFIGS.keys())}")
        return 1, None

    config = CONFIGS[config_name]

    os.environ["CONFIG_NAME"] = config_name
    for key, value in config.items():
        os.environ[key] = value

    if retry_failed:
        logger.info(f"Starting retry mode for: {config_name}")
    elif reevaluate_custom:
        logger.info(f"Starting custom filter reevaluation for: {config_name}")
        logger.debug(f"Sheet ID: {config['SHEET_ID']}")
        logger.debug(f"Job Listings URL: {config['JOB_LISTINGS_URL']}")
    elif apply_filter:
        logger.info(f"Applying filter '{apply_filter}' for: {config_name}")
        logger.debug(f"Sheet ID: {config['SHEET_ID']}")
        logger.debug(f"Job Listings URL: {config['JOB_LISTINGS_URL']}")
    else:
        logger.info(f"Starting tracker: {config_name}")
        logger.debug(f"Sheet ID: {config['SHEET_ID']}")
        logger.debug(f"Job Listings URL: {config['JOB_LISTINGS_URL']}")

    try:
        summary = pittcsc_simplify.main(
            retry_failed=retry_failed,
            reevaluate_custom=reevaluate_custom,
            apply_filter=apply_filter,
        )
        if retry_failed:
            logger.info(f"Retry mode completed: {config_name}")
            return 0, None
        elif reevaluate_custom:
            logger.info(f"Custom filter reevaluation completed: {config_name}")
            return 0, None
        elif apply_filter:
            logger.info(f"Apply filter completed: {config_name}")
            return 0, None
        elif summary:
            logger.info(f"Successfully completed: {config_name}")
            return 0, summary
        else:
            logger.error(f"No summary returned for {config_name}")
            return 1, None
    except KeyboardInterrupt:
        logger.warning("Tracker interrupted by user")
        return 130, None
    except Exception as e:
        logger.error(f"Tracker failed for {config_name}: {e}")
        return 1, None


def print_aggregate_summary(summaries: List[RunSummary]) -> None:
    logger.info("")
    logger.info("=" * 60)
    logger.info("AGGREGATE SUMMARY")
    logger.info("=" * 60)

    total_fetched = sum(s["total_fetched"] for s in summaries)
    total_added = sum(s["added_to_sheet"] for s in summaries)
    total_ai_filtered = sum(s["ai_filtered"] for s in summaries)

    logger.info(f"Total Jobs Fetched Across All Configs: {total_fetched}")
    logger.info(f"Total Jobs Added to Sheets: {total_added}")
    logger.info(f"Total Jobs AI Filtered: {total_ai_filtered}")
    logger.info("")

    for summary in summaries:
        logger.info(f"{summary['config_name']}:")
        logger.info(f"  ├─ Fetched: {summary['total_fetched']}")
        logger.info(f"  ├─ Added: {summary['added_to_sheet']}")
        logger.info(f"  └─ AI Filtered: {summary['ai_filtered']}")

    logger.info("=" * 60)


def run_configs(config_names: List[str]) -> int:
    failed_configs: List[str] = []
    summaries: List[RunSummary] = []

    logger.info("Running all configurations sequentially (limited by OpenAI API rate limits)")

    for config in config_names:
        exit_code, summary = run_tracker(config)
        if exit_code != 0:
            failed_configs.append(config)
            logger.warning(f"Continuing despite failure in {config}")
        elif summary:
            summaries.append(summary)

    if summaries:
        print_aggregate_summary(summaries)

    if failed_configs:
        logger.error(f"Failed configurations: {', '.join(failed_configs)}")
        return 1

    logger.info("All configurations completed successfully")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Run job tracker for various configurations")
    parser.add_argument("config_name", help=f"Configuration to run: {', '.join(CONFIGS.keys())}; groups: {', '.join(load_groups().keys())}")
    parser.add_argument("--retry", action="store_true", help="Retry all failed jobs from ai_results.db")
    parser.add_argument("--reevaluate-custom", action="store_true", help="Reevaluate all custom filter jobs and update sheet")
    parser.add_argument("--batch", action="store_true", help="Use the OpenAI Batch API (50%% cheaper, async) for AI checks")
    parser.add_argument("--limit", type=int, default=None, help="Only AI-check the first N unseen jobs (for testing)")
    parser.add_argument("--apply-filter", default=None, help="Apply a named filter from filters.toml to cached jobs and add passing ones to the sheet")
    args = parser.parse_args()

    if args.batch:
        os.environ["BATCH_MODE"] = "1"
    if args.limit is not None:
        os.environ["PROCESS_LIMIT"] = str(args.limit)

    config_name = args.config_name.lower()

    groups = load_groups()
    if config_name in groups:
        return run_configs(groups[config_name])

    exit_code, _ = run_tracker(
        config_name,
        retry_failed=args.retry,
        reevaluate_custom=args.reevaluate_custom,
        apply_filter=args.apply_filter,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())