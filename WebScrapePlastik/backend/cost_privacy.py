"""Harga modal (cost) is admin-only.

Pure ASGI middleware: for a logged-in non-admin, every JSON response under /api has the cost
fields zeroed before it leaves the server. Doing it here instead of per route means a new
endpoint that returns a master item or an inbound document is covered the moment it exists.
The UI also hides these fields; this is the part that actually keeps the numbers private.
"""

import json

from backend.auth import SESSION_COOKIE_NAME, hash_token
from backend.database import get_session

COST_KEYS = {"last_cost_price", "avg_cost_price", "cost_price", "total_inventory_value", "total_value",
             "unit_cost", "cost_value", "writeoffs_cost_value"}


def _strip(node):
    if isinstance(node, dict):
        return {k: (0 if k in COST_KEYS and isinstance(v, (int, float)) else _strip(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _is_staff(scope) -> bool:
    cookies = {}
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                cookies[k] = v
    token = cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    try:
        session = get_session(hash_token(token))
    except Exception:
        return False
    return bool(session) and session["role"] != "admin"


class CostPrivacyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/") or not _is_staff(scope):
            await self.app(scope, receive, send)
            return

        start = None
        chunks: list[bytes] = []

        async def capture(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                headers = dict(message.get("headers", []))
                if b"application/json" not in headers.get(b"content-type", b""):
                    await send(message)
                    start = None  # pass-through mode
                return
            if start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            try:
                body = json.dumps(_strip(json.loads(body)), ensure_ascii=False).encode()
            except Exception:
                pass
            headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture)
