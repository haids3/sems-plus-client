"""Errors raised by the SEMS+ client."""


class SemsPlusError(Exception):
    """Base class for every SEMS+ client error."""


class SemsPlusConnectionError(SemsPlusError):
    """The API could not be reached or returned something unreadable."""


class SemsPlusAuthError(SemsPlusError):
    """The credentials were rejected."""


class SemsPlusPermissionError(SemsPlusError):
    """The account may not read or control this station or device."""


class SemsPlusApiError(SemsPlusError):
    """The API answered with an error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code


class SemsPlusRateLimitError(SemsPlusError):
    """The API asked us to slow down; no request is sent until the pause ends."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"SEMS+ rate limited; retry after {retry_after}s")
        self.retry_after = retry_after
