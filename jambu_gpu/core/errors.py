"""Normalized error model (spec section 22).

Provider adapters MUST translate their native failures into these types so the
CLI behaves identically regardless of which provider is configured.
"""

from __future__ import annotations


class JambuError(Exception):
    """Base class for every error raised by the CLI."""

    exit_code = 1


class ConfigError(JambuError):
    """jambu.yaml (or legacy config.yml) is missing, malformed or semantically invalid."""

    exit_code = 2


class StateError(JambuError):
    """Local runtime state is missing or inconsistent."""

    exit_code = 3


class ProviderError(JambuError):
    """Base class for every provider-originated failure."""

    exit_code = 4


class AuthenticationError(ProviderError):
    exit_code = 5


class CapacityUnavailableError(ProviderError):
    exit_code = 6


class ProvisioningError(ProviderError):
    exit_code = 7


class ProviderTimeoutError(ProviderError):
    exit_code = 8


class UnsupportedCapabilityError(ProviderError):
    exit_code = 9


class BudgetExceededError(JambuError):
    exit_code = 10


class RuntimeStartupError(JambuError):
    exit_code = 11


class WorkloadFailed(JambuError):
    """The user workload exited non-zero. Carries the original exit code."""

    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.exit_code = returncode or 1
