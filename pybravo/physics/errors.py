"""Physical rehearsal diagnostics without importing the optional native SDK."""

from typing import Any


class PhysicalSimulationError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}
