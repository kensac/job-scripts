export PYTHONPATH := src

.PHONY: api worker check lint fmt types test testdb-up testdb-down testdb-sync testdb-sync-fast integration schema migrate revision db-up db-down

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

testdb-up:      ## docker postgres WITH pgvector on :55432, for the test suite
	docker run -d --rm --name jobtracker-testdb -p 55432:5432 \
	  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=jobtracker_test \
	  pgvector/pgvector:pg18-trixie >/dev/null
	@until docker exec jobtracker-testdb pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
	@echo 'export TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/jobtracker_test'

testdb-down:    ## stop it
	docker stop jobtracker-testdb >/dev/null 2>&1 || true

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
