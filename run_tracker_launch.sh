#!/bin/bash

# Make sure HOME is set
export HOME="/Users/kanishk"

# Optional: set a reasonable PATH (not strictly needed now, but good hygiene)
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Go to the project directory
cd /Users/kanishk/personal/job-scripts || exit 1

# Use the exact python from your job-scripts env
/Users/kanishk/.pyenv/versions/job-scripts/bin/python run_tracker.py all
