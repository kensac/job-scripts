import json

from api.app import app

print(json.dumps(app.openapi(), indent=2))
