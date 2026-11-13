"""Media-layer failures that are independent of HTTP concerns."""

from __future__ import annotations


class MediaOperationFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LocalReadUnavailable(RuntimeError):
    """Raised when a ranged/local read cannot service a plan.

    This is an internal control-flow signal, not a client-facing rejection:
    callers must fall back to the full read-and-decode path so observable
    behavior never changes. A source that is truncated or has a layout the
    ranged parser cannot prove equivalent to the full parser lands here.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
