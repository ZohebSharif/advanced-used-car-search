from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ExtractionSuggestion(BaseModel):
    """Untrusted suggestions. Deterministic extraction decides which values are usable."""

    model_config = ConfigDict(extra="forbid", strict=True)

    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    price: int | None = Field(default=None, ge=0)
    fees: int | None = Field(default=None, ge=0)
    mileage: int | None = Field(default=None, ge=0)
    location: str | None = None
    exterior: str | None = None
    interior: str | None = None
    vin: str | None = None
    title_evidence: str | None = None
    history: str | None = None
    seller_type: str | None = None
    cpo: bool | None = None
    posted_date: str | None = None
    description: str | None = None
    seller_name: str | None = None


@dataclass(frozen=True)
class ModelCall:
    status: str
    model: str
    latency_ms: int
    input_chars: int
    attempt_count: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    detail: str = ""

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class ModelClient(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def model_name(self) -> str: ...

    def extract(self, visible_text: str, url: str) -> tuple[ExtractionSuggestion | None, ModelCall]: ...


class DisabledModelClient:
    def __init__(self, model_name: str, detail: str = "model disabled or API key absent"):
        self._model_name = model_name
        self._detail = detail

    @property
    def available(self) -> bool:
        return False

    @property
    def model_name(self) -> str:
        return self._model_name

    def extract(self, visible_text: str, url: str) -> tuple[None, ModelCall]:
        return None, ModelCall("unavailable", self.model_name, 0, 0, 0, detail=self._detail)


class DeepSeekClient:
    TRANSIENT = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError, TimeoutError)

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client_factory: Callable[..., Any] = OpenAI,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._enabled = bool(config["model_enabled"])
        self._api_key = os.getenv("DEEPSEEK_API_KEY")
        self._base_url = str(config["model_base_url"])
        self._model_name = str(config["model_name"])
        self._timeout = float(config["model_timeout_seconds"])
        self._max_input_chars = int(config["model_max_input_chars"])
        self._max_output_tokens = int(config["model_max_output_tokens"])
        self._factory = client_factory
        self._sleeper = sleeper

    @property
    def available(self) -> bool:
        return self._enabled and bool(self._api_key)

    @property
    def model_name(self) -> str:
        return self._model_name

    def extract(self, visible_text: str, url: str) -> tuple[ExtractionSuggestion | None, ModelCall]:
        if not self.available:
            return DisabledModelClient(self.model_name).extract(visible_text, url)
        clipped = visible_text[: self._max_input_chars]
        started = time.monotonic()
        attempts = 0
        try:
            client = self._factory(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=0,
            )
            response = None
            for attempt in range(2):
                attempts = attempt + 1
                try:
                    response = client.chat.completions.create(
                        model=self.model_name,
                        temperature=0,
                        max_tokens=self._max_output_tokens,
                        response_format={"type": "json_object"},
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Return one valid JSON object only. Extract explicit visible "
                                    "listing text; use null for unknown. Never infer title, history, "
                                    "VIN, color, location, price, mileage, or vehicle identity from "
                                    "silence. The JSON keys are: year, make, model, trim, price, fees, "
                                    "mileage, location, exterior, interior, vin, title_evidence, "
                                    "history, seller_type, cpo, posted_date, description, seller_name."
                                ),
                            },
                            {"role": "user", "content": f"URL: {url}\nVisible text:\n{clipped}"},
                        ],
                    )
                    break
                except self.TRANSIENT:
                    if attempt == 1:
                        raise
                    self._sleeper(0.25)
            if response is None:
                raise RuntimeError("model returned no response")
            content = response.choices[0].message.content
            if not content:
                raise ValueError("empty model response")
            suggestion = ExtractionSuggestion.model_validate(json.loads(content))
            usage = getattr(response, "usage", None)
            return suggestion, ModelCall(
                "used",
                self.model_name,
                round((time.monotonic() - started) * 1000),
                len(clipped),
                attempts,
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
            )
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError, KeyError, IndexError) as exc:
            status = "invalid-output"
            detail = type(exc).__name__
        except self.TRANSIENT as exc:
            status = "transient-failure"
            detail = type(exc).__name__
        except Exception as exc:  # Provider failures are isolated; deterministic extraction continues.
            status = "failed"
            detail = type(exc).__name__
        return None, ModelCall(
            status,
            self.model_name,
            round((time.monotonic() - started) * 1000),
            len(clipped),
            attempts,
            detail=detail,
        )


class ModelBudget:
    def __init__(self, client: ModelClient, max_calls: int):
        self.client = client
        self.max_calls = max_calls
        self.calls: list[ModelCall] = []

    def extract(self, visible_text: str, url: str) -> ExtractionSuggestion | None:
        if not self.client.available:
            return None
        if len(self.calls) >= self.max_calls:
            return None
        suggestion, event = self.client.extract(visible_text, url)
        self.calls.append(event)
        return suggestion

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.client.available,
            "model": self.client.model_name,
            "calls": len(self.calls),
            "max_calls": self.max_calls,
            "statuses": [event.public_dict() for event in self.calls],
            "total_latency_ms": sum(event.latency_ms for event in self.calls),
            "total_input_chars": sum(event.input_chars for event in self.calls),
        }
