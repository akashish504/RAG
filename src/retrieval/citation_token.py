"""Opaque, HMAC-signed citation tokens for the ``/cite/{token}`` endpoint.

A citation link is ``https://<host>/cite/<token>`` where ``token`` is a
tamper-proof, self-contained encoding of a single S3 object (bucket + key) plus
an expiry. The endpoint verifies the token and serves the object (redirect or
proxy). This keeps citation URLs short (~70 chars), exposes no AWS internals,
scopes access to exactly one object, and is revocable by rotating the secret.

Format (all base64url, no padding):
    token = b64(payload) + "." + b64(HMAC_SHA256(b64(payload), secret))
    payload = "<bucket>\\n<key>\\n<exp_epoch_seconds>"

Pure stdlib — no third-party dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(digest)


def make_citation_token(
    s3_key: str,
    *,
    bucket: str,
    ttl_seconds: int,
    secret: str,
    now: int | None = None,
) -> str:
    """Create a signed token granting time-bounded access to one S3 object."""
    exp = (now if now is not None else int(time.time())) + int(ttl_seconds)
    payload = f"{bucket}\n{s3_key}\n{exp}"
    payload_b64 = _b64e(payload.encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_citation_token(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> tuple[str, str] | None:
    """Return ``(bucket, key)`` for a valid token, or ``None`` if tampered/expired.

    Validates the HMAC in constant time and checks expiry. Never raises.
    """
    try:
        payload_b64, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    expected_sig = _sign(payload_b64, secret)
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        bucket, key, exp_str = _b64d(payload_b64).decode("utf-8").split("\n", 2)
        exp = int(exp_str)
    except (ValueError, UnicodeDecodeError):
        return None
    current = now if now is not None else int(time.time())
    if current >= exp:
        return None
    if not bucket or not key:
        return None
    return bucket, key
