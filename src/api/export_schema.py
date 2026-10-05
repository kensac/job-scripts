import json
import os

# The export reads the route table, never a table in the database. Importing
# the app opens the pool, so point it at a closed port: the export then needs
# no database and cannot reach whatever DATABASE_URL the shell holds. The pool
# logs that it could not connect and the output is byte-identical.
os.environ["DATABASE_URL"] = "postgresql://unused:unused@127.0.0.1:1/no_database_here"

from api.app import app

print(json.dumps(app.openapi(), indent=2))
