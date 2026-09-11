export PYTHONPATH := src

.PHONY: sync api worker check lint fmt types test dev-api dev-headers testdb-up testdb-down testdb-url testdb-sync testdb-sync-fast integration corpus profile profile-check schema migrate revision db-up db-down

sync:           ## install exactly the lockfile into .venv (then activate it)
	uv sync --frozen

api:            ## run the API locally
	uvicorn api.app:app --port 8000 --reload

worker:         ## run a worker locally
	python -m api.worker

check:          ## everything CI gates on: lint, format, types, compile, tests
	ruff check src tests
	ruff format --check src tests
	pyright
	PYTHONPATH=src lint-imports
	python -m compileall -q src
	@if git grep -InF -e "—" -e "\\u2014" -- . ':!Makefile' ':!extension/ats/*' ; then echo "em dash found: write a comma, a colon, or a new sentence"; exit 1; fi
	node --test tests/extension/*.test.cjs
	pytest -q tests

lint:           ## report lint findings (add ARGS=--fix to apply)
	ruff check src tests $(ARGS)

fmt:            ## format the codebase
	ruff format src tests

types:          ## type-check the live code (src/api, src/core)
	pyright

test:           ## run the test suite
	node --test tests/extension/*.test.cjs
	pytest -q tests

schema:         ## regenerate openapi.json (commit it)
	python -m api.export_schema > openapi.json

migrate:        ## apply migrations to $$DATABASE_URL
	python -m alembic upgrade head

revision:       ## autogenerate a migration: make revision m="add foo"
	python -m alembic revision --autogenerate -m "$(m)"

# One test container PER CHECKOUT. A single shared name and port is not a
# nuisance, it is a correctness problem: `docker run --rm --name X` from a
# second worktree replaces the first one's database while its suite is still
# running, and the tests fail as "server closed the connection unexpectedly"
# or as a TRUNCATE against tables that no longer exist. That reads as a flaky
# test rather than as a environment being pulled out from underneath it, and
# four parallel checkouts each diagnosed it separately before anyone noticed
# the common cause - one of them watching a 717-row table become a database
# that did not exist.
#
# Derived from the checkout path rather than a hand-set suffix, so it is stable
# across runs in one worktree, distinct between worktrees, and needs nobody to
# remember to set anything. The port is derived the same way; a collision binds
# loudly instead of silently sharing.
# The NAME carries the checkout's directory so `docker ps` says whose it is -
# four sessions each debugged this separately partly because the containers
# were indistinguishable. Two worktrees with the same basename would collide,
# and that fails loudly on the docker run rather than silently sharing.
TESTPG_NAME := jobtracker-testdb-$(notdir $(CURDIR))
# The PORT is hashed from the full path, because the name alone does not fix
# anything: a second container with a unique name still cannot bind a port the
# first one holds, which is what pushed sessions into hand-picking numbers and
# left one with no published port at all. A 1000-wide range keeps collisions
# unlikely, and a collision binds loudly instead of sharing quietly.
TESTPG_PORT := $(shell echo $$((55000 + 0x$(shell pwd | shasum | cut -c1-6) % 1000)))
TESTPG_URL := postgresql://postgres:test@127.0.0.1:$(TESTPG_PORT)/jobtracker_test

testdb-up:      ## docker postgres WITH pgvector for THIS checkout's test suite
	docker run -d --rm --name $(TESTPG_NAME) -p $(TESTPG_PORT):5432 \
	  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=jobtracker_test \
	  pgvector/pgvector:pg18-trixie >/dev/null
	@until docker exec $(TESTPG_NAME) pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; do sleep 1; done
	@echo 'export TEST_DATABASE_URL=$(TESTPG_URL)'

testdb-down:    ## stop this checkout's test database
	docker stop $(TESTPG_NAME) >/dev/null 2>&1 || true

testdb-url:     ## print this checkout's TEST_DATABASE_URL
	@echo 'export TEST_DATABASE_URL=$(TESTPG_URL)' 

# Autogenerate connects to a database to diff the models against it, and bare
# `alembic revision --autogenerate` reads DATABASE_URL, which is PRODUCTION.
# It is a read, so nothing was harmed the day this was found, but the next
# command in that shell is `alembic upgrade` and that one is not. Generate
# against the throwaway copy, always.
migration:      ## generate a migration from the models (m="what changed")
	@test -n "$(m)" || { echo 'usage: make migration m="what changed"'; exit 1; }
	DATABASE_URL='$(TESTPG_URL)' alembic upgrade head
	DATABASE_URL='$(TESTPG_URL)' alembic revision --autogenerate -m "$(m)"

# --- dev API ------------------------------------------------------------
# A real API over a THROWAWAY COPY of production, so the frontend can build
# against real shapes. The mock layer this replaces produced a 422 on every
# resolve assignment, four envelope-key mismatches and an "infinite append"
# bug, all because a fixture cannot falsify the assumption it was built from.
# A copy cannot get the shape wrong, because it is the shape - including the
# awkward cases nobody has found yet.
#
# THE ISOLATION IS A CREDENTIAL, NOT A NETWORK. jobtracker-db is deliberately
# published on the public internet, which is how the oci and desktop workers
# reach it, so "it runs locally" buys nothing: a process handed the production
# DSN connects from anywhere. What keeps this off production is that the dev
# role cannot log in to it.
#
# Setup, once:
#   set -a && . ./.env && set +a
#   python scripts/sync_testdb.py --name jobtracker_test --dev-role jobtracker_dev
#   # then export the JOBTRACKER_DEV_DATABASE_URL it prints
#
# Refreshing is DELIBERATELY a command and not a schedule. A copy that goes
# stale without anyone noticing is the fixture problem again, one layer out,
# so the staleness is at least attributable to the last time someone ran it.
#
# WITHOUT A PRODUCTION CREDENTIAL, which is what anyone working in
# personal-portfolio actually wants:
#
#   make testdb-up && eval "$(make testdb-url)"
#   make corpus
#   JOBTRACKER_DEV_DATABASE_URL=$TEST_DATABASE_URL make dev-api
#
# The corpus is generated from a committed measurement of production, so the
# shapes are real without any of the data being. It also holds five users with
# overlapping boards, which the synced copy never will - production has one
# user, so a copy cannot show whether a screen renders the right person's
# rows.
DEV_API_PORT ?= 8000

dev-api:        ## run the API against the throwaway copy (needs JOBTRACKER_DEV_DATABASE_URL)
	@test -n "$$JOBTRACKER_DEV_DATABASE_URL" || { \
	  echo "JOBTRACKER_DEV_DATABASE_URL is not set."; \
	  echo "Create the role and copy first:"; \
	  echo "  python scripts/sync_testdb.py --name jobtracker_test --dev-role jobtracker_dev"; \
	  exit 1; }
	@python -m core.disposable_db --env JOBTRACKER_DEV_DATABASE_URL --allow-dev
	DATABASE_URL="$$JOBTRACKER_DEV_DATABASE_URL" JOBTRACKER_SERVICE_TOKEN=dev-token \
	  uvicorn api.app:app --port $(DEV_API_PORT) --reload

dev-headers:    ## print the identity headers a dev client must send
	@echo '# API on http://127.0.0.1:$(DEV_API_PORT). Send:'
	@echo '#   X-Service-Token: dev-token'
	@echo '#   X-User-Sub: <the sub of a user in the copy>'
	@echo '#   X-User-Email: <their email>'
	@echo '#   X-User-Groups: infra-admins,jobtracker-users-internal'
	@echo '# Any authenticated request PROVISIONS a user row if the sub is new,'
	@echo '# so use a sub that already exists in the copy unless you mean to.'

db-up:          ## throwaway local postgres on :54999 (data in .pgdev)
	initdb -D .pgdev -U dev --auth=trust -E UTF8 >/dev/null 2>&1 || true
	pg_ctl -D .pgdev -o "-p 54999 -c unix_socket_directories=''" start
	createdb -h 127.0.0.1 -p 54999 -U dev jobtracker_dev 2>/dev/null || true
	@echo 'export DATABASE_URL=postgresql://dev@127.0.0.1:54999/jobtracker_dev'

db-down:        ## stop and delete the throwaway postgres
	pg_ctl -D .pgdev stop -m fast || true
	rm -rf .pgdev

# --- test database -------------------------------------------------------
# A copy of production on the same Postgres instance, refreshed on demand so
# integration tests run against real shapes instead of invented ones. Manual
# by design: prod data only moves when you ask it to.
#
# pg_dump must match the server major version and the local client is older,
# so the dump runs inside a postgres:18 container rather than pinning a second
# client install on every machine.
TESTDB ?= jobtracker_test

testdb-sync:    ## rebuild $(TESTDB) from production (destructive)
	python scripts/sync_testdb.py --name $(TESTDB)

testdb-sync-fast: ## same, minus the large text columns (~80% of the data)
	python scripts/sync_testdb.py --name $(TESTDB) --fast

integration:    ## the tests that need a synced copy of REAL production
	pytest -q -m integration tests

# --- the generated corpus ------------------------------------------------
# A catalog shaped like production and containing none of it, so the whole
# suite runs on a pull request with no production credential anywhere. The
# shapes are measured, not invented: `make profile` reads production and
# rewrites tests/production_profile.json, and the scheduled `--check` fails
# when production grows a shape the generator cannot produce.
#
# The suite builds this on demand, so there is normally nothing to run here.
# It is a target because a dev API over generated data needs no secret at all,
# which the synced copy will always need.

# One line of python, not a backslash-continued block: make passes the
# continuation lines' leading TAB to the shell, and /bin/sh hands it to python
# as an unexpected indent.
corpus:         ## fill TEST_DATABASE_URL with the generated corpus
	@test -n "$$TEST_DATABASE_URL" || { \
	  echo 'TEST_DATABASE_URL is not set. Start a database and export it:'; \
	  echo '  make testdb-up && eval "$$(make testdb-url)"'; exit 1; }
	@test -n "$$APP_ENCRYPTION_KEY" || { \
	  echo 'APP_ENCRYPTION_KEY is not set. The corpus writes REAL ciphertext for'; \
	  echo 'user_oauth_tokens and user_settings, so the paths that decrypt a token'; \
	  echo 'are exercised rather than mocked - which means the API you point at'; \
	  echo 'this database afterwards must hold the same key. Generate one and keep'; \
	  echo 'it for both:'; \
	  echo '  export APP_ENCRYPTION_KEY=$$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")'; \
	  exit 1; }
	@DATABASE_URL="$$TEST_DATABASE_URL" JOBTRACKER_SERVICE_TOKEN=corpus python -c "import tests.corpus as c; [print(f'  {k}: {v}') for k, v in sorted(c.build().items())]"

profile:        ## re-measure production into tests/production_profile.json
	python scripts/measure_profile.py

profile-check:  ## fail if production holds a shape the corpus cannot produce
	python scripts/measure_profile.py --check
