"""Decode the digest encodings accepted by source and stored WARC validators."""

import base64
import hashlib


def parse_warc_digest(value: str):
    """Return a fresh hasher and supplied digest bytes, or raise ValueError."""
    algorithm, separator, expected = value.partition(":")
    if not separator:
        raise ValueError("missing digest algorithm")
    digester = hashlib.new(algorithm.lower())
    if len(expected) == digester.digest_size * 2:
        supplied = bytes.fromhex(expected)
    elif len(expected) == len(base64.b32encode(digester.digest())):
        supplied = base64.b32decode(expected.upper())
    else:
        supplied = base64.b64decode(expected, altchars=b"-_", validate=True)
    return digester, supplied
