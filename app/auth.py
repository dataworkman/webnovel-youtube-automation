from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import Response

log = logging.getLogger("uvicorn.error")


def install_auth(app: FastAPI) -> None:
    password = os.getenv("APP_PASSWORD", "")
    username = os.getenv("APP_USER", "admin")
    if not password:
        log.warning("APP_PASSWORD is not set: the app (including paid API actions) is open to anyone who can reach this port.")
        return
    expected = (username.encode("utf-8"), password.encode("utf-8"))

    @app.middleware("http")
    async def require_basic_auth(request: Request, call_next):
        if _authorized(request.headers.get("authorization", ""), expected):
            return await call_next(request)
        return Response("Authentication required.", status_code=401, headers={"WWW-Authenticate": 'Basic realm="webnovel-youtube-automation", charset="UTF-8"'})


def _authorized(header: str, expected: tuple[bytes, bytes]) -> bool:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, secret = base64.b64decode(token.strip(), validate=True).partition(b":")
    except (binascii.Error, ValueError):
        return False
    user_ok = hmac.compare_digest(user, expected[0])
    secret_ok = hmac.compare_digest(secret, expected[1])
    return user_ok and secret_ok
