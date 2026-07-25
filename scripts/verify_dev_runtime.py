"""Non-destructive HTTP checks for the running C2 development runtime."""

import json
import os
import urllib.request


base_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8101").rstrip("/")


def fetch(path: str) -> tuple[int, bytes, str]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
        return response.status, response.read(), response.headers.get_content_type()


status, body, content_type = fetch("/healthz")
assert status == 200
assert json.loads(body) == {"ok": True, "service": "stockwicks-commercial-client"}
assert content_type == "application/json"

status, body, content_type = fetch("/auth/login")
assert status == 200
assert content_type == "text/html"
assert b"<html" in body.lower()

status, body, content_type = fetch("/static/css/style.css")
assert status == 200
assert body
assert content_type == "text/css"

print("C2 HTTP smoke checks passed.")
