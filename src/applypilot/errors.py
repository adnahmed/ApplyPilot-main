"""Machine-readable failure metadata, independent of user-facing messages."""


class AuthorizationError(RuntimeError):
    """An operation requires authorization from the owner."""

    error_kind = "auth"

