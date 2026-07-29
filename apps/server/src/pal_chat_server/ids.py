from __future__ import annotations

import os
from datetime import UTC, datetime

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def generate_ulid(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    timestamp_ms = int(current.timestamp() * 1000)
    entropy = int.from_bytes(os.urandom(10), "big")
    payload = (timestamp_ms << 80) | entropy
    return _encode_base32(payload, 26)
