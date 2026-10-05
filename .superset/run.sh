#!/usr/bin/env bash
# Dev API over this workspace's generated corpus, never production. Named
# explicitly so a JOBTRACKER_DEV_DATABASE_URL in the shell cannot redirect it.
set -euo pipefail
./.superset/devdb.sh
source .venv/bin/activate
set -a; source .env.dev; set +a
eval "$(make -s testdb-url)"

# A free port per run, so parallel workspaces never fight over 8000.
port=$(python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
make -s dev-headers DEV_API_PORT="$port"
JOBTRACKER_DEV_DATABASE_URL="$TEST_DATABASE_URL" exec make dev-api DEV_API_PORT="$port"
