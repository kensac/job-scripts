export PYTHONPATH := src

.PHONY: api worker check test schema migrate revision db-up db-down

api:            ## run the API locally
	uvicorn api.app:app --port 8000 --reload

worker:         ## run a worker locally
	python -m api.worker

check:          ## compile-check every source file
	python -m compileall -q src && echo OK

test:           ## run the test suite
	pytest -q tests

schema:         ## regenerate openapi.json (commit it)
	python -m api.export_schema > openapi.json

migrate:        ## apply migrations to $$DATABASE_URL
	python -m alembic upgrade head

revision:       ## autogenerate a migration: make revision m="add foo"
	python -m alembic revision --autogenerate -m "$(m)"

db-up:          ## throwaway local postgres on :54999 (data in .pgdev)
	initdb -D .pgdev -U dev --auth=trust -E UTF8 >/dev/null 2>&1 || true
	pg_ctl -D .pgdev -o "-p 54999 -c unix_socket_directories=''" start
	createdb -h 127.0.0.1 -p 54999 -U dev jobtracker_dev 2>/dev/null || true
	@echo 'export DATABASE_URL=postgresql://dev@127.0.0.1:54999/jobtracker_dev'

db-down:        ## stop and delete the throwaway postgres
	pg_ctl -D .pgdev stop -m fast || true
	rm -rf .pgdev
