"""Shared protection for authoritative frozen V1.1 contracts."""

from __future__ import annotations

from typing import Any


class NoUncheckedCopyUpdates:
    """Forbid Pydantic's unvalidated ``model_copy(update=...)`` escape hatch."""

    def model_copy(
        self, *, update: dict[str, Any] | None = None, deep: bool = False
    ) -> Any:
        if update:
            raise TypeError(
                f"{type(self).__name__} does not permit public model_copy updates"
            )
        return super().model_copy(deep=deep)


__all__ = ["NoUncheckedCopyUpdates"]
