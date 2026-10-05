#!/usr/bin/env bash
# New workspace: copy local-only files from the root checkout, install
# dependencies, and build this workspace's throwaway database.
set -euo pipefail

for f in .env .python-version .claude/settings.local.json configs.toml filters.toml; do
  if [ -f "$SUPERSET_ROOT_PATH/$f" ] && [ ! -e "$f" ]; then
    mkdir -p "$(dirname "$f")"
    cp "$SUPERSET_ROOT_PATH/$f" "$f"
  fi
done

uv sync --frozen
npm --prefix extension ci

./.superset/devdb.sh
