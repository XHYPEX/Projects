"""Idempotency for state-changing POSTs.

Pure ASGI middleware, like backend/activity.py: a new route is covered the moment it exists. A POST
that sends an `Idempotency-Key` header is run once; the same key on the same path and session returns
the stored response (header `Idempotent-Replay: true`) instead of running again. A request that
arrives while the first is still running gets 409 and can be retried. Only successful (2xx)
responses are kept; a failed attempt releases the key so the corrected retry is a fresh one.
"""

import asyncio

from backend.auth import SESSION_COOKIE_NAME
from backend.database import claim_idempotency_key, finish_idempotency_key, release_idempotency_key


def _cookie(headers: dict[bytes, bytes], name: str) -> str:
    for part in headers.get(b"cookie", b"").decode("latin-1").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


class IdempotencyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        key = headers.get(b"idempotency-key", b"").decode("latin-1").strip()
        if not key or len(key) > 120:
            return await self.app(scope, receive, send)

        owner = _cookie(headers, SESSION_COOKIE_NAME)
        scope_id = f"{owner[:16]}|{scope['path']}|{key}"
        loop = asyncio.get_event_loop()
        state, stored = await loop.run_in_executor(None, claim_idempotency_key, scope_id)

        if state == "done":
            await send({"type": "http.response.start", "status": stored["status"], "headers": [
                (b"content-type", (stored["content_type"] or "application/json").encode()),
                (b"idempotent-replay", b"true"),
            ]})
            return await send({"type": "http.response.body", "body": stored["body"] or b""})
        if state == "busy":
            body = b'{"detail":"Permintaan yang sama masih diproses. Tunggu sebentar."}'
            await send({"type": "http.response.start", "status": 409, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})

        captured = {"status": 500, "content_type": None, "chunks": []}

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        captured["content_type"] = v.decode("latin-1")
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, capture)
        except BaseException:
            await loop.run_in_executor(None, release_idempotency_key, scope_id)
            raise
        if 200 <= captured["status"] < 300:
            await loop.run_in_executor(
                None, finish_idempotency_key, scope_id, captured["status"], captured["content_type"], b"".join(captured["chunks"])
            )
        else:
            await loop.run_in_executor(None, release_idempotency_key, scope_id)
