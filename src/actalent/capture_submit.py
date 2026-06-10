from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from actalent import actalent_apply as aa
from core.paths import DATA_DIR

OUTPUT = str(DATA_DIR / "capture_submit.json")


def grab_network(driver) -> list:
    reqs: dict = {}
    events: list = []
    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Network.requestWillBeSent":
            r = params.get("request", {})
            reqs[params.get("requestId")] = {"url": r.get("url"), "method": r.get("method")}
        elif method == "Network.responseReceived":
            resp = params.get("response", {})
            info = reqs.get(params.get("requestId"), {})
            events.append({
                "method": info.get("method"),
                "url": resp.get("url"),
                "status": resp.get("status"),
            })
    return events


def main() -> int:
    profile = aa.load_profile()
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url:
        pending = [u for u in aa.load_apply_urls() if u not in aa.load_progress()]
        if not pending:
            sys.exit("No pending apply URLs in CSV.")
        url = pending[0]

    options = Options()
    options.add_argument("--window-size=1400,2200")
    options.add_experimental_option("detach", True)
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = webdriver.Chrome(options=options)

    print(f"Opening + auto-filling:\n  {url}\n")
    aa.apply_to_job(driver, url, profile)

    baseline_url = driver.current_url
    driver.get_log("performance")  # flush baseline traffic so we only capture the submit

    print("\n" + "=" * 70)
    print("Now in the browser: finish the manual fields (country/state/title),")
    print("click SUBMIT, and WAIT until you see the result/confirmation page.")
    print("THEN come back here and press Enter to capture.")
    print("=" * 70)
    input("Press Enter AFTER you see the post-submit result... ")

    time.sleep(1.0)
    final_url = driver.current_url
    body_text = driver.execute_script("return document.body.innerText;") or ""
    network = grab_network(driver)
    posts = [e for e in network if e.get("method") == "POST"]

    keywords = [
        "thank you", "thanks for applying", "application submitted", "submitted",
        "successfully", "received", "we have received", "confirmation", "all set",
        "complete", "next steps",
    ]
    low = body_text.lower()
    hits = [k for k in keywords if k in low]

    capture = {
        "apply_url": url,
        "baseline_url": baseline_url,
        "final_url": final_url,
        "url_changed": final_url != baseline_url,
        "confirmation_keyword_hits": hits,
        "body_text_snippet": body_text[:1200],
        "post_requests": posts,
        "all_responses_count": len(network),
    }
    Path(OUTPUT).write_text(json.dumps(capture, indent=2))

    print("\n--- CAPTURE SUMMARY ---")
    print("url changed:", capture["url_changed"])
    print("baseline:", baseline_url[:90])
    print("final:   ", final_url[:90])
    print("confirmation keywords found:", hits)
    print(f"POST requests captured: {len(posts)}")
    for p in posts:
        print(f"  [{p['status']}] {p['url'][:110]}")
    print(f"\nFull capture written to {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
