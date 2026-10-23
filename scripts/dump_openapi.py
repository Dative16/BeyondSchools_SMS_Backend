"""Dump the OpenAPI schema so the web repo can generate its types."""

import json

from app.main import app

if __name__ == "__main__":
    print(json.dumps(app.openapi(), indent=2))
