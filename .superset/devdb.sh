#!/usr/bin/env bash
# Make sure this checkout's test database is up and holds the generated corpus.
# The container and its port come from `make testdb-up`, which derives both from
# the checkout path, so workspaces never share one. The container is --rm, so
# after a Docker restart this rebuilds it.
set -euo pipefail
source .venv/bin/activate
export PYTHONPATH=src

# The corpus writes real ciphertext, so the API must hold the same key.
# Kept per workspace in .env.dev (gitignored by the .env.* rule).
if [ ! -f .env.dev ]; then
  echo "APP_ENCRYPTION_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" > .env.dev
fi
set -a; source .env.dev; set +a

eval "$(make -s testdb-url)"
name="jobtracker-testdb-$(basename "$PWD")"
if [ -z "$(docker ps -q -f name="^${name}$")" ]; then
  make -s testdb-up >/dev/null
  DATABASE_URL="$TEST_DATABASE_URL" python -m alembic upgrade head
  make -s corpus
fi
