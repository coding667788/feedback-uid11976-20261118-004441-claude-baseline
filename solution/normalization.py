"""Login name normalization rules.

Two policy versions exist and never change once a tenant is created:

* ``V1`` -- ``NFC(raw_login)``, compared case-sensitively.
* ``V2`` -- ``NFKC(NFKC(raw_login).casefold())``.

No whitespace stripping, pinyin conversion or visual-similarity replacement
is performed. A name is rejected when, **before or after** normalization,
it:

* is empty;
* contains any character for which :py:meth:`str.isspace` is true;
* contains a character of Unicode general category ``Cc``, ``Cf`` or ``Cs``;
* has a normalized UTF-8 encoding longer than :data:`MAX_UTF8_BYTES`.
"""

from __future__ import annotations

import unicodedata

from .errors import InvalidLogin

V1 = "V1"
V2 = "V2"
POLICIES = (V1, V2)

MAX_UTF8_BYTES = 128
_FORBIDDEN_CATEGORIES = frozenset(("Cc", "Cf", "Cs"))


def _check_stage(value: str, stage: str) -> None:
    """Validate one stage (raw input or normalized result)."""
    if not isinstance(value, str):
        raise InvalidLogin(
            "login name must be a string",
            details={"stage": stage, "reason": "not_a_string"},
        )
    if value == "":
        raise InvalidLogin(
            "empty login name", details={"stage": stage, "reason": "empty"}
        )
    for ch in value:
        if ch.isspace():
            raise InvalidLogin(
                "whitespace characters are not allowed in login names",
                details={"stage": stage, "reason": "whitespace", "char": ch},
            )
        if unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            raise InvalidLogin(
                "control/format/surrogate characters are not allowed",
                details={
                    "stage": stage,
                    "reason": "forbidden_category",
                    "category": unicodedata.category(ch),
                },
            )
    if len(value.encode("utf-8")) > MAX_UTF8_BYTES:
        raise InvalidLogin(
            f"normalized login name exceeds {MAX_UTF8_BYTES} UTF-8 bytes",
            details={
                "stage": stage,
                "reason": "too_long",
                "utf8_bytes": len(value.encode("utf-8")),
            },
        )


def normalize(raw_login: str, policy: str) -> str:
    """Return the canonical login name under *policy*.

    Raises :class:`~solution.errors.InvalidLogin` for any rule violation.
    """
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    # Validation on the raw value: normalization must not launder illegal
    # characters away (e.g. a soft hyphen U+00AD is Cf and must fail even
    # though NFKC drops it).
    _check_stage(raw_login, "raw")
    if policy == V1:
        normalized = unicodedata.normalize("NFC", raw_login)
    else:
        # NFKC first, then casefold, then NFKC again on the folded result.
        normalized = unicodedata.normalize(
            "NFKC", unicodedata.normalize("NFKC", raw_login).casefold()
        )
    _check_stage(normalized, "normalized")
    return normalized


def unidata_version() -> str:
    """Unicode database version the running interpreter uses."""
    return unicodedata.unidata_version
