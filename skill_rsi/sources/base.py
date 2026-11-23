"""The cursor and raw source contract for batch and follow readers."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Origin:
    source_path: str
    generation: str
    locator: dict
    content: bytes
    format: str = "json"
    byte_start: int | None = None
    byte_end: int | None = None

    def position(self) -> dict:
        return {
            "path": self.source_path, "generation": self.generation, "locator": self.locator,
            "byte_start": self.byte_start, "byte_end": self.byte_end,
        }


class DataSource(Protocol):
    def read(self, cursor: Mapping | None = None) -> Iterator[tuple[dict | None, Origin, dict]]:
        """Yield a record, its immutable source bytes, and the next checkpoint."""
        ...


class SourceRegistry:
    """Register reader factories explicitly without importing code from record data."""

    def __init__(self):
        self._readers = {}

    def register(self, name, factory):
        if not isinstance(name, str) or not name or not callable(factory) or name in self._readers:
            raise ValueError(f"Invalid or duplicate source reader: {name!r}")
        self._readers[name] = factory

    def open(self, name, **options):
        if name not in self._readers:
            raise ValueError(f"Unknown source reader: {name!r}")
        return self._readers[name](**options)
