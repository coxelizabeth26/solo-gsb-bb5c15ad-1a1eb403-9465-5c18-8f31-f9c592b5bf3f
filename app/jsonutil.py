"""JSON helpers shared by the merge and patch services."""

from __future__ import annotations

from typing import Any


class _Missing:
    """Sentinel for an absent key, kept distinct from a JSON null value."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


MISSING = _Missing()


def json_equal(a: Any, b: Any) -> bool:
    """Structural JSON equality.

    Unlike plain ``==`` this keeps the JSON types distinct that Python folds
    together (``True`` is not ``1``), while still treating ``1`` and ``1.0``
    as the same JSON number.
    """
    if a is MISSING or b is MISSING:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return a == b
