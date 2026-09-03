export PYTHONPATH := src

.PHONY: api worker check lint fmt types test dev-up dev-api dev-url dev-down testdb-up testdb-down testdb-url testdb-sync testdb-sync-fast integration schema migrate revision db-up db-down

api:            ## run the API locally
	uvicorn api.app:app --port 8000 --reload

worker:         ## run a worker locally
	python -m api.worker

check:          ## everything CI gates on: lint, format, types, compile, tests
	ruff check src tests
	ruff format --check src tests
	pyright
	python -m compileall -q src
	pytest -q tests

lint:           ## report lint findings (add ARGS=--fix to apply)
	ruff check src tests $(ARGS)

fmt:            ## format the codebase
	ruff format src tests

types:          ## type-check the live code (src/api, src/core)
	pyright

test:           ## run the test suite
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
	@until docker exec $(TESTPG_NAME) pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
	@echo 'export TEST_DATABASE_URL=$(TESTPG_URL)'

testdb-down:    ## stop this checkout's test database
	docker stop $(TESTPG_NAME) >/dev/null 2>&1 || true

testdb-url:     ## print this checkout's TEST_DATABASE_URL
	@echo 'export TEST_DATABASE_URL=$(TESTPG_URL)' 

# --- dev API ------------------------------------------------------------
# A real API over a disposable database, so the frontend can verify against
# real response shapes. The mock layer it replaces produced a 422 on every
# resolve assignment, four envelope-key mismatches and an "infinite append"
# bug, all because a fixture cannot falsify the assumption it was built from.
#
# Same port derivation as the test database and a different offset, so a dev
# API and a test run in one checkout do not fight over a port.
DEVPG_NAME := jobtracker-devdb-$(shell pwd | shasum | cut -c1-8)
DEVPG_PORT := $(shell echo $$((56000 + 0x$(shell pwd | shasum | cut -c1-6) % 1000)))
DEVPG_URL := postgresql://postgres:dev@127.0.0.1:$(DEVPG_PORT)/jobtracker_dev
DEV_API_PORT ?= 8000

dev-up:         ## disposable postgres, migrated and seeded with real shapes
	docker run -d --rm --name $(DEVPG_NAME) -p $(DEVPG_PORT):5432 \
	  -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=jobtracker_dev \
	  pgvector/pgvector:pg18-trixie >/dev/null
	@# pg_isready reports ready DURING init, before the server accepts real
	@# connections - the first run of this target failed on exactly that. Wait
	@# for a query to succeed, which is the thing alembic is about to do.
	@until docker exec $(DEVPG_NAME) psql -U postgres -d jobtracker_dev -c 'select 1' \
	  >/dev/null 2>&1; do sleep 1; done
	@DATABASE_URL=$(DEVPG_URL) python -m alembic upgrade head >/dev/null
	@DATABASE_URL=$(DEVPG_URL) python -c "import core.store" >/dev/null
	@DATABASE_URL=$(DEVPG_URL) python -c "from core.devseed import seed; print(seed())"
	@echo 'export DATABASE_URL=$(DEVPG_URL)'

dev-api:        ## run the API against the dev database (needs dev-up)
	DATABASE_URL=$(DEVPG_URL) JOBTRACKER_SERVICE_TOKEN=dev-token \
	  uvicorn api.app:app --port $(DEV_API_PORT) --reload

dev-url:        ## print this checkout's dev DATABASE_URL and the headers to send
	@echo 'export DATABASE_URL=$(DEVPG_URL)'
	@echo '# API on http://127.0.0.1:$(DEV_API_PORT), send these headers:'
	@echo '#   X-Service-Token: dev-token'
	@echo '#   X-User-Sub: dev-user'
	@echo '#   X-User-Email: dev@example.test'
	@echo '#   X-User-Groups: infra-admins,jobtracker-users-internal'

dev-down:       ## stop this checkout's dev database
	@# -f rather than stop: a container left behind by a failed dev-up blocks
	@# the next one on a name conflict, which is how the first run of this
	@# target wasted a cycle.
	docker rm -f $(DEVPG_NAME) >/dev/null 2>&1 || true

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

integration:    ## run integration tests (needs TEST_DATABASE_URL on a *_test db)
	pytest -q -m integration tests
