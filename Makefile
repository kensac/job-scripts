export PYTHONPATH := src

.PHONY: sync check prose migrations-check lint fmt types coverage test test-par dev-api dev-worker dev-headers testdb-up testdb-down testdb-url testdb-sync testdb-sync-fast integration corpus profile profile-check schema migrate migration

sync:           ## install exactly the lockfile into .venv (then activate it)
	uv sync --frozen

# The same gates as .github/workflows/ci.yml. Where a gate is more than one
# command it is a target here that CI calls, so the two cannot drift apart.
check:          ## everything CI gates on: lint, format, types, compile, prose, migrations, extension, tests
	ruff check src tests
	ruff format --check src tests
	pyright
	PYTHONPATH=src lint-imports
	python -m compileall -q src
	$(MAKE) prose
	$(MAKE) migrations-check
	npm --prefix extension ci
	npm --prefix extension run typecheck
	npm --prefix extension run build
	npm --prefix extension test
	$(MAKE) test-par

prose:          ## fail on an em dash anywhere in the repository
	@if git grep -InF -e "—" -e "\\u2014" -- . ':!Makefile' ':!extension/adapters/recipes/*' ; then echo "em dash found: write a comma, a colon, or a new sentence"; exit 1; fi

# Applies every migration, then fails if the models describe a schema the
# migrations do not produce. Two heads from parallel branches fail the apply.
# Reads TEST_DATABASE_URL, else this checkout's test container, and refuses a
# database not named like a disposable one.
migrations-check: ## migrations apply and the models match them
	@url="$${TEST_DATABASE_URL:-$(TESTPG_URL)}"; \
	TEST_DATABASE_URL="$$url" python -m core.disposable_db --env TEST_DATABASE_URL || exit 1; \
	DATABASE_URL="$$url" python -m alembic upgrade head || exit 1; \
	DATABASE_URL="$$url" python -m alembic check || { \
	  echo 'The ORM models and the migrations disagree. Write the migration:'; \
	  echo '  make migration m="what changed"'; exit 1; }

lint:           ## report lint findings (add ARGS=--fix to apply)
	ruff check src tests $(ARGS)

fmt:            ## format the codebase
	ruff format src tests

types:          ## type-check the live code (src/api, src/core, src/tasks)
	pyright

coverage:       ## measure test coverage (never gated, see docs/agents)
	PYTHONPATH=src pytest -q tests --cov=src --cov-report=term-missing:skip-covered

test:           ## run the test suite
	npm --prefix extension run build
	npm --prefix extension test
	pytest -q tests

test-par:       ## run the python suite across cores (one database per worker)
	pytest -q -n auto tests

schema:         ## regenerate openapi.json (commit it)
	python -m api.export_schema > openapi.json

migrate:        ## apply migrations to the dev database (production migrates itself on start)
	@$(DEV_DB_GUARD)
	DATABASE_URL="$$JOBTRACKER_DEV_DATABASE_URL" python -m alembic upgrade head

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

# Local commands reach this checkout's throwaway database and nothing else.
# The application reads DATABASE_URL from its environment only, never from
# .env, and these targets set it from JOBTRACKER_DEV_DATABASE_URL, which
# defaults to the test database above. Point it at the synced copy's dev role
# to use real rows. Production's DSN is PRODUCTION_DATABASE_URL in .env, read
# only by the targets that say production: profile, profile-check, testdb-sync.
dev-api dev-worker migrate: export JOBTRACKER_DEV_DATABASE_URL := $(or $(JOBTRACKER_DEV_DATABASE_URL),$(TESTPG_URL))
# Refuses any database not named like a disposable one (*_test, *_dev, ...).
DEV_DB_GUARD = python -m core.disposable_db --env JOBTRACKER_DEV_DATABASE_URL --allow-dev

# Idempotent: already up on this checkout's port is nothing to do. A container
# with this name on another port is another checkout's, and the run fails on
# the name rather than sharing it.
testdb-up:      ## docker postgres WITH pgvector for THIS checkout's test suite
	@docker port $(TESTPG_NAME) 5432/tcp 2>/dev/null | grep -q ':$(TESTPG_PORT)$$' || \
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
# `alembic revision --autogenerate` reads whatever DATABASE_URL the shell
# holds. When .env held production under that name, this was a read of
# production, and the next command in that shell was `alembic upgrade`, which
# is not a read. Generate against the throwaway copy, always.
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
# Setup, once (reads PRODUCTION_DATABASE_URL from .env):
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
#   make dev-api
#
# The corpus is generated from a committed measurement of production, so the
# shapes are real without any of the data being. It also holds five users with
# overlapping boards, which the synced copy never will - production has one
# user, so a copy cannot show whether a screen renders the right person's
# rows.
DEV_API_PORT ?= 8000

dev-api:        ## run the API against the dev database (migrates it on start)
	@$(DEV_DB_GUARD)
	DATABASE_URL="$$JOBTRACKER_DEV_DATABASE_URL" JOBTRACKER_SERVICE_TOKEN=dev-token \
	  uvicorn api.app:app --port $(DEV_API_PORT) --reload

dev-worker:     ## run a worker against the dev database
	@$(DEV_DB_GUARD)
	DATABASE_URL="$$JOBTRACKER_DEV_DATABASE_URL" python -m api.worker

dev-headers:    ## print the identity headers a dev client must send
	@echo '# API on http://127.0.0.1:$(DEV_API_PORT). Send:'
	@echo '#   X-Service-Token: dev-token'
	@echo '#   X-User-Sub: <the sub of a user in the copy>'
	@echo '#   X-User-Email: <their email>'
	@echo '#   X-User-Groups: infra-admins,jobtracker-users-internal'
	@echo '# Any authenticated request PROVISIONS a user row if the sub is new,'
	@echo '# so use a sub that already exists in the copy unless you mean to.'

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
