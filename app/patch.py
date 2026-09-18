"""RFC 6902 JSON Patch evaluation.

The :func:`apply_patch` entry point runs a list of ``add`` / ``remove`` /
``replace`` / ``test`` operations against a *deep copy* of the document, in
array order. Two failure classes are distinguished:

* :class:`PatchStructureError` -- the request itself is invalid (malformed
  operation object, bad JSON Pointer syntax, illegal array-index token); the
  service answers ``422``.
* :class:`PatchConflict` -- the request is well formed but cannot apply
  (target does not exist, array index out of bounds, ``test`` comparison
  fails); the service answers ``409`` and reports ``failed_index``/``path``/
  ``reason``. Since the operations execute on a deep copy, a conflict never
  leaks partially applied results.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .jsonutil import MISSING, json_equal

SUPPORTED_OPS = ("add", "remove", "replace", "test")


class PatchStructureError(Exception):
    """Raised for an invalid operation structure or JSON Pointer (422)."""

    def __init__(self, reason: str, index: int = -1, path: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.index = index
        self.path = path


class PatchConflict(Exception):
    """Raised when a valid operation cannot apply (409)."""

    def __init__(self, reason: str, index: int, path: str):
        super().__init__(reason)
        self.reason = reason
        self.index = index
        self.path = path


def parse_pointer(pointer: str, index: int) -> list[str]:
    """Parse an RFC 6901 JSON Pointer into its raw (unescaped) tokens.

    ``""`` denotes the whole document and yields no tokens. Any other pointer
    must begin with ``/``. ``~`` may only be followed by ``0`` or ``1``.
    """
    if not isinstance(pointer, str):
        raise PatchStructureError("'path' must be a JSON Pointer string", index)
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchStructureError(
            "malformed JSON Pointer: non-empty pointer must start with '/'",
            index,
            pointer,
        )
    tokens: list[str] = []
    for raw in pointer[1:].split("/"):
        token = []
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "~":
                if i + 1 >= len(raw) or raw[i + 1] not in "01":
                    raise PatchStructureError(
                        "malformed JSON Pointer: '~' must be followed by '0' or '1'",
                        index,
                        pointer,
                    )
                token.append("~" if raw[i + 1] == "0" else "/")
                i += 2
            else:
                token.append(ch)
                i += 1
        tokens.append("".join(token))
    return tokens


def _check_structure(operations: list) -> None:
    """Validate operation objects independently of the document."""
    if not isinstance(operations, list):
        raise PatchStructureError("'operations' must be an array")
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            raise PatchStructureError("each operation must be an object", index)
        name = op.get("op", MISSING)
        if name is MISSING:
            raise PatchStructureError("missing 'op' field", index, op.get("path"))
        if name not in SUPPORTED_OPS:
            raise PatchStructureError(
                f"unsupported op {name!r}; supported ops are: "
                "add, remove, replace, test",
                index,
                op.get("path"),
            )
        path = op.get("path", MISSING)
        if path is MISSING or not isinstance(path, str):
            raise PatchStructureError("missing or non-string 'path' field", index)
        # Parsed up front so syntactically bad pointers fail before any
        # operation touches the document.
        parse_pointer(path, index)
        if "value" in op:
            if name == "remove":
                raise PatchStructureError(
                    "'remove' must not carry a 'value' field", index, path
                )
        elif name in ("add", "replace", "test"):
            raise PatchStructureError(
                f"'{name}' requires a 'value' field", index, path
            )
        tokens = parse_pointer(path, index)
        if tokens and tokens[-1] == "-" and name != "add":
            raise PatchStructureError(
                "the '-' token (append marker) may only be used with 'add'",
                index,
                path,
            )


def _array_index(token: str, index: int, path: str, for_add: bool) -> int:
    """Resolve an array reference token to an integer index.

    ``-`` is only legal for ``add`` and maps to one past the end. Any other
    non-numeric token, or one with a leading zero, is a malformed pointer
    (422); an index outside the array bounds is a conflict (409).
    """
    if token == "-":
        if not for_add:
            raise PatchStructureError(
                "the '-' token (append marker) may only be used with 'add'",
                index,
                path,
            )
        return -1  # caller maps it to len(array)
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise PatchStructureError(
            f"malformed array index {token!r}", index, path
        )
    return int(token)


def _descend(parent: Any, token: str, index: int, path: str) -> Any:
    """Walk one pointer token while resolving a parent path.

    The token must name an existing child which itself is navigable; only
    numeric tokens (never ``-``) are accepted on arrays.
    """
    if isinstance(parent, list):
        if token == "-":
            raise PatchConflict(
                "'-' points past the last array element and cannot be traversed",
                index,
                path,
            )
        if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
            raise PatchStructureError(
                f"malformed array index {token!r}", index, path
            )
        i = int(token)
        if i >= len(parent):
            raise PatchConflict(
                f"array index {i} out of bounds (length {len(parent)})",
                index,
                path,
            )
        return parent[i]
    if isinstance(parent, dict):
        if token not in parent:
            raise PatchConflict(
                f"object has no key {token!r}", index, path
            )
        return parent[token]
    raise PatchConflict(
        f"cannot traverse {type(parent).__name__} with pointer token {token!r}",
        index,
        path,
    )


def apply_patch(document: Any, operations: list) -> Any:
    """Validate and atomically apply ``operations`` to a copy of document.

    Returns the resulting document. Raises :class:`PatchStructureError` for a
    bad request and :class:`PatchConflict` when a valid request cannot apply.
    """
    _check_structure(operations)
    result = deepcopy(document)

    for index, op in enumerate(operations):
        name = op["op"]
        path: str = op["path"]
        value = op.get("value", MISSING)
        tokens = parse_pointer(path, index)

        # Operations on the whole document (the root pointer "").
        if not tokens:
            if name == "add":
                result = deepcopy(value)
            elif name == "remove":
                raise PatchConflict(
                    "'remove' cannot target the whole document", index, path
                )
            elif name == "replace":
                result = deepcopy(value)
            else:  # test
                if not json_equal(result, value):
                    raise PatchConflict(
                        "test failed: target value differs from 'value'",
                        index,
                        path,
                    )
            continue

        # Resolve the parent; every intermediate token must exist.
        parent = result
        traversed = ""
        for token in tokens[:-1]:
            traversed += "/" + token.replace("~", "~0").replace("/", "~1")
            parent = _descend(parent, token, index, traversed)

        token = tokens[-1]
        if isinstance(parent, dict):
            if name == "add":
                parent[token] = deepcopy(value)
            elif name == "remove":
                if token not in parent:
                    raise PatchConflict(
                        f"object has no key {token!r}", index, path
                    )
                del parent[token]
            elif name == "replace":
                if token not in parent:
                    raise PatchConflict(
                        f"object has no key {token!r}", index, path
                    )
                parent[token] = deepcopy(value)
            else:  # test
                if token not in parent:
                    raise PatchConflict(
                        f"object has no key {token!r}", index, path
                    )
                if not json_equal(parent[token], value):
                    raise PatchConflict(
                        "test failed: target value differs from 'value'",
                        index,
                        path,
                    )
        elif isinstance(parent, list):
            i = _array_index(token, index, path, for_add=(name == "add"))
            if name == "add":
                if i == -1:
                    parent.append(deepcopy(value))
                elif i > len(parent):
                    raise PatchConflict(
                        f"array index {i} out of bounds for insertion "
                        f"(length {len(parent)})",
                        index,
                        path,
                    )
                else:
                    parent.insert(i, deepcopy(value))
            else:
                if i >= len(parent):
                    raise PatchConflict(
                        f"array index {i} out of bounds (length {len(parent)})",
                        index,
                        path,
                    )
                if name == "remove":
                    del parent[i]
                elif name == "replace":
                    parent[i] = deepcopy(value)
                else:  # test
                    if not json_equal(parent[i], value):
                        raise PatchConflict(
                            "test failed: target value differs from 'value'",
                            index,
                            path,
                        )
        else:
            raise PatchConflict(
                f"cannot resolve pointer within {type(parent).__name__}",
                index,
                path,
            )

    return result
