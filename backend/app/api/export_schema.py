"""Export the application contract without starting services or touching data."""
import json
import sys

from app.api.main import create_app


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(create_app().openapi(), ensure_ascii=False))
