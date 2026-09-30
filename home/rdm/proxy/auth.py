"""Pure bearer-authorization decision helpers for the ingress proxy."""

import hashlib
import hmac

_SCHEME = b"bearer"
_OWS = b" \t"


def _digest(token: bytes) -> bytes:
    return hashlib.sha256(token).digest()


def constant_time_equal(left: bytes, right: bytes) -> bool:
    # Hash both sides to fixed-width digests so comparison time never leaks token length.
    return hmac.compare_digest(_digest(left), _digest(right))


def check_token(headers: list[tuple[bytes, bytes]], expected: bytes) -> bool:
    try:
        if not expected:
            return False
        values = [value for name, value in headers if name == b"authorization"]
        if len(values) != 1:
            return False
        parts = values[0].split(None, 1)
        if len(parts) != 2 or parts[0].lower() != _SCHEME:
            return False
        token = parts[1].strip(_OWS)
        if not token:
            return False
        return constant_time_equal(token, expected)
    except TypeError:
        return False