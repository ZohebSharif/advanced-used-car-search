from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from advanced_used_car_search.config import load
from advanced_used_car_search.model import DeepSeekClient, ModelBudget
from openai import APIStatusError, AuthenticationError, PermissionDeniedError, RateLimitError


def response(payload: dict):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeFactory:
    def __init__(self, outcomes):
        self.completions = FakeCompletions(outcomes)
        self.instances = []

    def __call__(self, **kwargs):
        self.instances.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=self.completions))


def enabled_config(**overrides):
    return load(
        model_enabled=True,
        model_max_input_chars=20,
        model_max_calls_per_run=1,
        model_timeout_seconds=2,
        **overrides,
    )


def test_model_environment_overrides_are_validated(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_MODEL", "custom-flash")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example/v1")
    monkeypatch.setenv("MODEL_ENABLED", "true")
    monkeypatch.setenv("MODEL_MAX_CALLS_PER_RUN", "2")
    config = load()
    assert config["model_name"] == "custom-flash"
    assert config["model_base_url"] == "https://deepseek.example/v1"
    assert config["model_enabled"] is True
    assert config["model_max_calls_per_run"] == 2


def test_model_is_disabled_without_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    factory = FakeFactory([response({})])
    client = DeepSeekClient(enabled_config(), client_factory=factory)
    budget = ModelBudget(client, 1)
    assert budget.extract("visible", "https://example.com/vehicle/1") is None
    assert factory.instances == []
    assert budget.summary()["calls"] == 0


def test_deepseek_json_contract_and_input_cap(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    factory = FakeFactory([response({"trim": "Luxury", "cpo": True})])
    client = DeepSeekClient(enabled_config(), client_factory=factory)
    suggestion, event = client.extract("x" * 100, "https://example.com/vehicle/1")
    assert suggestion and suggestion.trim == "Luxury" and suggestion.cpo is True
    assert event.status == "used" and event.input_chars == 20 and event.attempt_count == 1
    assert factory.instances[0]["base_url"] == "https://api.deepseek.com"
    assert factory.completions.calls[0]["model"] == "deepseek-flash"
    assert factory.completions.calls[0]["response_format"] == {"type": "json_object"}
    assert "secret-for-test" not in json.dumps(event.public_dict())


def test_deepseek_transport_rejects_redirects_and_environment_proxies(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    options = {}

    class Transport:
        def __init__(self) -> None:
            self.is_closed = False

        def close(self) -> None:
            self.is_closed = True

    transport = Transport()

    def transport_factory(**kwargs):
        options.update(kwargs)
        return transport

    monkeypatch.setattr("advanced_used_car_search.model.httpx.Client", transport_factory)
    factory = FakeFactory([response({"trim": "Luxury"})])
    suggestion, event = DeepSeekClient(enabled_config(), client_factory=factory).extract(
        "visible", "https://example.com/vehicle/1"
    )

    assert suggestion and event.status == "used"
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert transport.is_closed


def test_transient_failure_retries_once(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    factory = FakeFactory([TimeoutError("temporary"), response({"trim": "Luxury"})])
    sleeps = []
    client = DeepSeekClient(enabled_config(), client_factory=factory, sleeper=sleeps.append)
    suggestion, event = client.extract("visible", "https://example.com/vehicle/1")
    assert suggestion and suggestion.trim == "Luxury"
    assert event.attempt_count == 2
    assert len(factory.completions.calls) == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    ("error_type", "status_code"),
    [
        (AuthenticationError, 401),
        (PermissionDeniedError, 403),
        (RateLimitError, 429),
    ],
)
def test_provider_access_restrictions_are_blocked_without_retry(
    monkeypatch, error_type, status_code: int
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    response_ = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )
    factory = FakeFactory(
        [
            error_type(
                "provider access restriction",
                response=response_,
                body=None,
            )
        ]
    )
    sleeps = []
    client = DeepSeekClient(enabled_config(), client_factory=factory, sleeper=sleeps.append)
    suggestion, event = client.extract("visible", "https://example.com/vehicle/1")

    assert suggestion is None
    assert event.status == "blocked"
    assert event.attempt_count == 1
    assert len(factory.completions.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_generic_provider_access_status_is_blocked_without_retry(
    monkeypatch, status_code: int
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    response_ = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )
    factory = FakeFactory(
        [APIStatusError("provider access restriction", response=response_, body=None)]
    )
    sleeps = []
    suggestion, event = DeepSeekClient(
        enabled_config(), client_factory=factory, sleeper=sleeps.append
    ).extract("visible", "https://example.com/vehicle/1")

    assert suggestion is None
    assert event.status == "blocked"
    assert event.attempt_count == 1
    assert len(factory.completions.calls) == 1
    assert sleeps == []


def test_provider_redirect_is_not_followed_or_retried(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    response_ = httpx.Response(
        302,
        headers={"location": "https://redirect.example/collect"},
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )
    factory = FakeFactory([APIStatusError("redirect", response=response_, body=None)])
    sleeps = []
    suggestion, event = DeepSeekClient(
        enabled_config(), client_factory=factory, sleeper=sleeps.append
    ).extract("visible", "https://example.com/vehicle/1")

    assert suggestion is None
    assert event.status == "failed"
    assert event.attempt_count == 1
    assert len(factory.completions.calls) == 1
    assert sleeps == []


def test_generic_provider_5xx_retries_once(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    response_ = httpx.Response(
        503,
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )
    factory = FakeFactory(
        [
            APIStatusError("temporary provider failure", response=response_, body=None),
            response({"trim": "Luxury"}),
        ]
    )
    sleeps = []
    suggestion, event = DeepSeekClient(
        enabled_config(), client_factory=factory, sleeper=sleeps.append
    ).extract("visible", "https://example.com/vehicle/1")

    assert suggestion and suggestion.trim == "Luxury"
    assert event.status == "used"
    assert event.attempt_count == 2
    assert len(factory.completions.calls) == 2
    assert sleeps == [0.25]


def test_invalid_or_extra_output_is_isolated(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    factory = FakeFactory([response({"trim": "Luxury", "invented": "field"})])
    suggestion, event = DeepSeekClient(enabled_config(), client_factory=factory).extract(
        "visible", "https://example.com/vehicle/1"
    )
    assert suggestion is None
    assert event.status == "invalid-output"


def test_model_budget_caps_calls(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-for-test")
    factory = FakeFactory([response({"trim": "Luxury"}), response({"trim": "Ultra Luxury"})])
    budget = ModelBudget(DeepSeekClient(enabled_config(), client_factory=factory), 1)
    assert budget.extract("visible", "https://example.com/vehicle/1") is not None
    assert budget.extract("visible", "https://example.com/vehicle/2") is None
    assert len(factory.completions.calls) == 1
