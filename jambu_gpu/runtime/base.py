"""Runtime contract (spec section 19).

Provider answers "where does the compute run?".
Runtime answers "how does the model run?".
Model answers "what model runs?".
These stay independent; a runtime never talks to a provider API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Type

from ..core.config import RuntimeConfig
from ..core.errors import ConfigError
from ..core.models import ValidationResult

ENDPOINT_NAME = "model"


class ModelRuntime(ABC):
    """Describes how to boot a model server inside the provisioned container."""

    engine: str = "abstract"
    default_image: str = ""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    # -- container shape ----------------------------------------------------

    def image(self) -> str:
        return self.config.compute.image or self.default_image

    def ports(self) -> list[int]:
        return [self.config.runtime.port]

    def env(self) -> dict[str, str]:
        return {}

    @abstractmethod
    def start_command(self) -> str:
        """Shell command that launches the model server in the background."""

    # -- health -------------------------------------------------------------

    def health_path(self) -> str:
        return self.config.health.path

    def health_url(self, base_url: str) -> str:
        return base_url.rstrip("/") + self.health_path()

    def base_url(self, endpoint_url: str) -> str:
        return endpoint_url.rstrip("/")

    def validate(self) -> ValidationResult:
        return ValidationResult()

    def describe(self) -> str:
        return f"{self.engine} :{self.config.runtime.port}"


class NoRuntime(ModelRuntime):
    """`runtime.engine: none` - provision bare compute, start nothing."""

    engine = "none"
    default_image = "pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime"

    def ports(self) -> list[int]:
        return []

    def start_command(self) -> str:
        return "true"

    def health_url(self, base_url: str) -> str:  # pragma: no cover - unused
        return base_url


class RuntimeRegistry:
    def __init__(self) -> None:
        self._runtimes: dict[str, Type[ModelRuntime]] = {"none": NoRuntime}

    def register(self, runtime_cls: Type[ModelRuntime]) -> Type[ModelRuntime]:
        self._runtimes[runtime_cls.engine] = runtime_cls
        return runtime_cls

    def names(self) -> list[str]:
        return sorted(self._runtimes)

    def get(self, config: RuntimeConfig, engine: Optional[str] = None) -> ModelRuntime:
        key = engine or config.runtime.engine
        try:
            return self._runtimes[key](config)
        except KeyError:
            known = ", ".join(self.names())
            raise ConfigError(f"unknown runtime engine '{key}' (available: {known})") from None


runtime_registry = RuntimeRegistry()
