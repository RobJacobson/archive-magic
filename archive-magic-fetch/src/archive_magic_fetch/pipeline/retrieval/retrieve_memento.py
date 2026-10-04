"""Request the exact original replay without nearby-capture substitution."""

from __future__ import annotations

from wayback import Memento, Mode, WaybackClient

from archive_magic_fetch.models import CaptureRef


def retrieve_memento(client: WaybackClient, capture: CaptureRef) -> Memento:
    identity = capture.identity
    return client.get_memento(
        identity.original_url,
        timestamp=identity.timestamp,
        mode=Mode.original,
        exact=True,
        follow_redirects=False,
    )
