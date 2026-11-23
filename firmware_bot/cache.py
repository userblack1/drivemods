"""Short-lived, exact-query cache of fully validated and downloaded packages."""
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from .response import SourceMessage


@dataclass
class Bundle:
    messages: list[SourceMessage]
    files: dict[int, Path]
    # Deliveries retain this object even when its cache entry is evicted.
    temporary: TemporaryDirectory

    @property
    def size(self):
        return sum(path.stat().st_size for path in self.files.values())


def request_version(request):
    from .versions import SoftwareVersion
    version = SoftwareVersion(request["query"], request["family"])
    return version.with_variant(request["variant"]) if request.get("variant") else version


def cache_key(version):
    # Never share across revisions, hardware variants, models or regions.
    return version.query, version.variant or ""


class BundleCache:
    def __init__(self, ttl, max_entries=32, max_bytes=256 * 1024 * 1024, clock=time.monotonic):
        self.ttl, self.max_entries, self.max_bytes, self.clock = ttl, max_entries, max_bytes, clock
        self.entries = OrderedDict()

    def prune(self):
        now = self.clock()
        for key, (expires, bundle, size) in list(self.entries.items()):
            if expires <= now or any(not path.is_file() for path in bundle.files.values()):
                del self.entries[key]

    def get(self, key):
        self.prune()
        entry = self.entries.get(key)
        if entry is None:
            return None
        self.entries.move_to_end(key)
        return entry[1]

    def put(self, key, bundle):
        self.prune()
        size = bundle.size
        if self.ttl <= 0 or size > self.max_bytes:
            return
        self.entries[key] = self.clock() + self.ttl, bundle, size
        self.entries.move_to_end(key)
        while len(self.entries) > self.max_entries or sum(entry[2] for entry in self.entries.values()) > self.max_bytes:
            self.entries.popitem(last=False)

    def clear(self):
        self.entries.clear()
