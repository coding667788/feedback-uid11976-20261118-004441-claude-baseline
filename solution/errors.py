"""Stable error codes used across the API and the JSON CLI.

Every error raised by the public API is an :class:`ToolError` carrying an
``error_code``. The CLI serializes failures as::

    {"ok": false, "error": {"code": "<ERROR_CODE>", "message": "..."}}
"""

from __future__ import annotations


class ToolError(Exception):
    """Base class for all domain errors. ``error_code`` is wire-stable."""

    error_code = "INTERNAL_ERROR"

    def __init__(self, message: str = "", *, details: object | None = None):
        super().__init__(message or self.error_code)
        self.message = message or self.error_code
        self.details = details

    def to_dict(self) -> dict:
        payload = {"code": self.error_code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class UnidataVersionMismatch(ToolError):
    """Stored unidata version differs from the running interpreter's."""

    error_code = "UNIDATA_VERSION_MISMATCH"


class TenantNotFound(ToolError):
    error_code = "TENANT_NOT_FOUND"


class TenantExists(ToolError):
    error_code = "TENANT_EXISTS"


class PolicyMismatch(ToolError):
    """An operation expected V1 (or V2) but the tenant uses another policy."""

    error_code = "POLICY_MISMATCH"


class AccountNotFound(ToolError):
    error_code = "ACCOUNT_NOT_FOUND"


class AccountExists(ToolError):
    error_code = "ACCOUNT_EXISTS"


class LoginConflict(ToolError):
    """The normalized login name is already taken inside the tenant."""

    error_code = "LOGIN_CONFLICT"


class InvalidLogin(ToolError):
    """Empty / whitespace / control / format / surrogate / too-long name."""

    error_code = "INVALID_LOGIN"


class StaleSnapshot(ToolError):
    """Source revision, policy or account set changed after plan generation."""

    error_code = "PLAN_STALE"


class PlanIdConflict(ToolError):
    """plan_id is already bound to different plan content."""

    error_code = "PLAN_ID_CONFLICT"


class PlanNotFound(ToolError):
    error_code = "PLAN_NOT_FOUND"


class PlanValidationError(ToolError):
    """Business-level rejection of a submitted plan (conflicts, bad alt...)."""

    error_code = "PLAN_VALIDATION_FAILED"


class SchemaError(ToolError):
    """Malformed JSON/arguments: duplicate/unknown fields, wrong types."""

    error_code = "SCHEMA_ERROR"
