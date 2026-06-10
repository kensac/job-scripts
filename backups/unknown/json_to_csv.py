import json
import csv

INPUT_FILE = "leetcode_problems.json"
OUTPUT_FILE = "leetcode_problems.csv"

with open(INPUT_FILE, "r") as f:
    data = json.load(f)

problems = data.get("problems", [])

if not problems:
    print("No problems found in JSON file")
    exit(1)

fieldnames = ["title", "url", "description", "source_url", "problem_type"]

with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()

    for problem in problems:
        row = {
            "title": problem.get("title", ""),
            "url": problem.get("url") or "",
            "description": problem.get("description") or "",
            "source_url": problem.get("source_url", ""),
            "problem_type": problem.get("problem_type", ""),
        }
        writer.writerow(row)

print(f"Converted {len(problems)} problems to {OUTPUT_FILE}")
