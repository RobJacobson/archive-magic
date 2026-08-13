"""Named acquisition, storage, and format policy constants."""

from pathlib import Path


DEFAULT_OUTPUT_ROOT = Path("../archives")
WARC_TARGET_BYTES = 1_000_000_000
PLAYBACK_WORKERS = 4
PLAYBACK_STARTS_PER_SECOND = 20.0
BACKPRESSURE_COOLDOWN_S = 60.0
MAX_PLAYBACK_ATTEMPTS = 4
CDX_PAGE_LIMIT = 10_000
DEFAULT_DATE_START = "19950101000000"
RUN_SCHEMA_VERSION = 2
WARC_VERSION = "1.1"
SOFTWARE_ID = "archive-magic-fetch/0.1.0"
USER_AGENT = (
    "archive-magic-fetch/0.1.0 "
    "(+https://github.com/RobJacobson/archive-magic)"
)

CDX_PAYLOAD_DIGEST_HEADER = "CDX-Payload-Digest"
CDX_STATUS_HEADER = "CDX-Status"
CDX_URLKEY_HEADER = "CDX-Urlkey"
CDX_DIGEST_MATCH_HEADER = "CDX-Digest-Match"
MISSING_CDX_PAYLOAD_DIGEST = "-"
MISSING_CDX_STATUS = "-"

# SHA-1 (CDX base32) of the literal IA playback stub body ``Invalid URI``.
INVALID_URI_PAYLOAD_DIGEST = "sha1:L4XNRRGWXWKNIAJFQOC6D2OULYFIDDTC"

# SHA-1 (CDX base32) of zero bytes.
EMPTY_PAYLOAD_DIGEST = "sha1:3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ"
