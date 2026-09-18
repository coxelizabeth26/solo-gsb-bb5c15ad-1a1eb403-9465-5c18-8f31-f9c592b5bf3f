"""RFC 8785 JSON Canonicalization Scheme (JCS).

:func:`canonicalize` serializes a JSON value parsed by :mod:`json` into
the deterministic, whitespace-free form defined by RFC 8785:

* object members are emitted recursively in ascending order of their
  names, compared as arrays of UTF-16 code units (Section 3.2.3);
* array element order is preserved;
* strings are UTF-8 with only the mandatory escapes (the control
  character shorthands ``\\b \\t \\n \\f \\r``, ``\\u00xx`` for the rest
  of U+0000..U+001F, and ``\\`` / ``"``), so ``/`` and non-ASCII code
  points are emitted as-is;
* numbers follow the ECMAScript ``Number.prototypeToString``
  shortest-round-trip form (Section 3.2.2.3 / ECMA-262 7.1.12.1), as
  used by ``JSON.stringify``. Python's :func:`float.__repr__` produces
  the identical shortest digit string; :func:`_serialize_number` only
  re-paginates it to match ECMAScript's exponent thresholds and strips
  Python's ``.0``/zero-padded exponents;
* ``true``, ``false`` and ``null`` are emitted literally and ``-0`` is
  serialized as ``0``.

Per the RFC, lone surrogates in strings and numbers that cannot be
expressed as IEEE 754 doubles (e.g. integers beyond ``1.7976...e308``)
are errors and raise :class:`CanonicalizationError`; the service maps
those to ``422``.
"""

from __future__ import annotations

import math
from typing import Any

# Escapes shared by string values and object member names. Every other
# code point (including "/") is emitted as-is.
_STRING_ESCAPES = {
    0x08: r"\b",
    0x09: r"\t",
    0x0A: r"\n",
    0x0C: r"\f",
    0x0D: r"\r",
    0x22: r"\"",
    0x5C: r"\\",
}


class CanonicalizationError(ValueError):
    """The document cannot be canonicalized under RFC 8785 (HTTP 422)."""


def _utf16_sort_key(s: str) -> bytes:
    """Return a key ordering ``s`` by unsigned UTF-16 code unit values.

    UTF-16-BE is byte-order free (big-endian), and code units are
    16-bit unsigned integers, so a plain byte comparison of the BE
    encoding is exactly the code-unit comparison the RFC mandates.
    """
    try:
        return s.encode("utf-16-be")
    except UnicodeEncodeError as exc:  # pragma: no cover - defensive
        raise CanonicalizationError(f"string cannot be encoded as UTF-16: {exc}")


def _serialize_string(s: str, out: list[str]) -> None:
    """Append a JSON string in the RFC 8785 escaping form."""
    out.append('"')
    for ch in s:
        cp = ord(ch)
        esc = _STRING_ESCAPES.get(cp)
        if esc is not None:
            out.append(esc)
        elif cp <= 0x1F:
            out.append(f"\\u{cp:04x}")
        elif 0xD800 <= cp <= 0xDFFF:
            # A well-formed str cannot contain a lone surrogate, but
            # reject explicitly as required by Section 3.2.2.2 rather
            # than emitting it into the canonical text.
            raise CanonicalizationError(
                "lone UTF-16 surrogate in string is not permitted by RFC 8785"
            )
        else:
            out.append(ch)
    out.append('"')


def _serialize_number(value: int | float) -> str:
    """Serialize a JSON number per RFC 8785 / ECMAScript 7.1.12.1.

    All JSON numbers are IEEE 754 doubles, so true integers are
    converted first (this rounds oversized integers the same way a JSON
    parser feeding V8 would); anything that becomes non-finite is
    rejected. The digits produced by ``repr(float)`` are the same
    shortest round-tripping digits V8 prints -- only the presentation
    (fixed vs. exponential, exponent padding) differs, and that is
    what this function re-lays-out.
    """
    if isinstance(value, int):
        try:
            value = float(value)
        except OverflowError:
            # The integer lies beyond the IEEE 754 double range; parsing
            # it the ECMAScript way would yield Infinity, which JSON
            # forbids.
            raise CanonicalizationError(
                "integer magnitude exceeds the IEEE 754 double range"
            )
    if not math.isfinite(value):
        raise CanonicalizationError(
            "NaN and Infinity are not permitted in JSON (RFC 8785 3.2.2.3)"
        )
    if value == 0.0:
        # Both +0 and -0 serialize as "0".
        return "0"

    r = repr(value)
    negative = r[0] == "-"
    if negative:
        r = r[1:]

    # Split repr() into significant digits and the decimal exponent n
    # such that value ~= digits * 10**(n-k), n = number of digits of
    # the integer part (i.e. floor(log10(abs(value))) + 1).
    if "e" in r:
        mantissa, exp_str = r.split("e")
        repr_exp = int(exp_str)
    else:
        mantissa, repr_exp = r, 0
    if "." in mantissa:
        int_part, frac_part = mantissa.split(".")
    else:
        int_part, frac_part = mantissa, ""
    frac_part = frac_part.rstrip("0")  # drop Python's trailing ".0"
    digits = int_part + frac_part
    n = repr_exp + len(int_part)
    k = len(digits)

    if n <= -6 or n >= 22:
        # Exponential notation: d.dddde±d with no exponent zero padding.
        body = digits[0]
        if k > 1:
            body += "." + digits[1:]
        e = n - 1
        body += "e" + ("+" if e >= 0 else "-") + str(abs(e))
    elif n <= 0:
        # 0.00...<digits>, with -n leading fractional zeros.
        body = "0." + "0" * (-n) + digits
    elif n >= k:
        # Digits followed by trailing zeros; integral values get no ".".
        body = digits + "0" * (n - k)
    else:
        # Decimal point falls inside the digits.
        body = digits[:n] + "." + digits[n:]

    return ("-" + body) if negative else body


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        _serialize_string(value, out)
    elif isinstance(value, (int, float)):
        out.append(_serialize_number(value))
    elif isinstance(value, list):
        out.append("[")
        for index, element in enumerate(value):
            if index:
                out.append(",")
            _serialize(element, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for index, key in enumerate(sorted(value, key=_utf16_sort_key)):
            if not isinstance(key, str):
                raise CanonicalizationError("object member names must be strings")
            if index:
                out.append(",")
            _serialize_string(key, out)
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    else:
        raise CanonicalizationError(
            f"value of type {type(value).__name__} is not representable as JSON"
        )


def canonicalize(document: Any) -> str:
    """Return the RFC 8785 canonical JSON text for ``document``.

    Raises :class:`CanonicalizationError` for data JCS cannot represent
    (lone surrogates, NaN/Infinity, non-JSON types).
    """
    out: list[str] = []
    _serialize(document, out)
    return "".join(out)
