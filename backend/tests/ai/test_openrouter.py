from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ai.model_registry import EnabledModel, ModelRegistry
from app.ai.openrouter import (
    DEFAULT_FALLBACK_MODELS,
    ModelNotAllowedError,
    OpenRouterClient,
    OpenRouterError,
    PrivacyConsentRequiredError,
    SpendCapExceededError,
)
from app.config import bootstrap_data_dir
from app.config_secrets import SecretsStore
from app.db import DatabaseManager


def _make_environment(tmp_path: Path):
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    secrets = SecretsStore(secrets_path=settings.secrets_yaml_path)
    secrets.update_secrets({"openrouter_api_key": "sk-or-v1-test-key-1234567890"})
    return settings, db, secrets


def _enabled(model_id: str) -> EnabledModel:
    return EnabledModel(
        id=model_id,
        name=model_id,
        context_length=128000,
        prompt_price_per_token=0.000003,
        completion_price_per_token=0.000015,
        supports_vision=False,
        verified=True,
    )


def _make_registry(db: DatabaseManager, enabled_ids: list[str]) -> ModelRegistry:
    registry = ModelRegistry(db)
    for model_id in enabled_ids:
        registry.enable(_enabled(model_id))
    return registry


@pytest.mark.asyncio
async def test_blocks_calls_until_privacy_consent_recorded_and_enforces_zdr_and_enabled_models(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = False
    registry = _make_registry(db, ["anthropic/claude-3.7-sonnet", "openai/gpt-4o"])

    captured_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "anthropic/claude-3.7-sonnet",
                            "name": "Claude 3.7 Sonnet",
                            "context_length": 200000,
                            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                        },
                        {
                            "id": "openai/gpt-4o",
                            "name": "GPT-4o",
                            "context_length": 128000,
                            "pricing": {"prompt": "0.0000025", "completion": "0.000010"},
                        },
                        {
                            "id": "mistralai/mistral-large",
                            "name": "Mistral Large",
                            "context_length": 128000,
                            "pricing": {"prompt": "0.000002", "completion": "0.000006"},
                        },
                    ]
                },
            )
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Grounded academic response."}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 80},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://openrouter.ai/api/v1") as http_client:
        client = OpenRouterClient(
            db=db,
            secrets=secrets,
            settings=settings,
            registry=registry,
            http_client=http_client,
        )

        # 1. Privacy consent is False initially -> must raise PrivacyConsentRequiredError
        with pytest.raises(PrivacyConsentRequiredError):
            await client.complete(
                model="anthropic/claude-3.7-sonnet",
                messages=[{"role": "user", "content": "Draft abstract."}],
            )
        assert len(captured_requests) == 0

        # 2. Record privacy consent -> now allowed
        settings.privacy_consent_accepted = True

        # 3. Catalog is unfiltered; enforcement happens against the registry
        models = await client.list_models()
        model_ids = [m.id for m in models]
        assert model_ids == [
            "anthropic/claude-3.7-sonnet",
            "openai/gpt-4o",
            "mistralai/mistral-large",
        ]

        # 4. Disallowed model is rejected before any network completion call
        with pytest.raises(ModelNotAllowedError):
            await client.complete(
                model="mistralai/mistral-large",
                messages=[{"role": "user", "content": "Try disallowed model."}],
            )
        assert len(captured_requests) == 0

        # 5. Chat, review, and vision calls enforce non-overridable ZDR provider routing
        # Even if caller passes extra_body attempting to override provider, ZDR remains enforced
        res_chat = await client.complete(
            model="anthropic/claude-3.7-sonnet",
            messages=[{"role": "user", "content": "Draft introduction."}],
            extra_body={"provider": {"data_collection": "allow", "zdr": False}},
        )
        assert res_chat.content == "Grounded academic response."
        assert captured_requests[-1]["provider"] == {
            "data_collection": "deny",
            "zdr": True,
        }

        # Vision call with PNG bytes also enforces ZDR and formats multimodal image_url
        fake_png = b"\x89PNG\r\n\x1a\nfakeimagebytes"
        res_vision = await client.complete(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "Inspect page layout."}],
            vision_images=[fake_png],
        )
        assert res_vision.content == "Grounded academic response."
        vision_payload = captured_requests[-1]
        assert vision_payload["provider"] == {
            "data_collection": "deny",
            "zdr": True,
        }
        last_msg_content = vision_payload["messages"][-1]["content"]
        assert isinstance(last_msg_content, list)
        assert any(part.get("type") == "image_url" for part in last_msg_content)


@pytest.mark.asyncio
async def test_atomic_reserve_spend_prevents_concurrent_gather_overspend(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    # Set tight caps: each request with max_tokens=2000 on default/configured pricing
    # Let's configure a known pricing or use reserve_spend / complete
    settings.request_spend_cap_usd = 0.50
    settings.daily_spend_cap_usd = 0.90

    network_calls = 0

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Reviewer output."}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 1000},
            },
        )

    transport = httpx.MockTransport(slow_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://openrouter.ai/api/v1") as http_client:
        client = OpenRouterClient(
            db=db,
            secrets=secrets,
            settings=settings,
            registry=_make_registry(db, ["anthropic/claude-3.7-sonnet"]),
            http_client=http_client,
        )

        # Single request exceeding REQUEST_SPEND_CAP_USD (0.50) is blocked immediately
        with pytest.raises(SpendCapExceededError):
            client.reserve_spend("anthropic/claude-3.7-sonnet", estimated_usd=0.60)

        # Now launch 4 concurrent complete() calls where each estimates ~$0.40
        # With daily_spend_cap_usd = 0.90, at most 2 can reserve ($0.40 + $0.40 = $0.80 <= $0.90),
        # while the other 2 must fail with SpendCapExceededError BEFORE hitting the network!
        client.set_model_pricing(
            "anthropic/claude-3.7-sonnet",
            prompt_price_per_token=0.0,
            completion_price_per_token=0.0002,  # 2000 max_tokens * 0.0002 = $0.40 estimated
        )

        tasks = [
            client.complete(
                model="anthropic/claude-3.7-sonnet",
                messages=[{"role": "user", "content": f"Concurrent review {i}"}],
                max_tokens=2000,
            )
            for i in range(4)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = [r for r in results if not isinstance(r, Exception)]
        cap_errors = [r for r in results if isinstance(r, SpendCapExceededError)]

        assert len(successes) == 2
        assert len(cap_errors) == 2
        assert network_calls == 2


def _catalog_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "anthropic/claude-3.7-sonnet", "name": "Claude 3.7 Sonnet"},
                    {"id": "openai/gpt-4o", "name": "GPT-4o"},
                    {"id": "mistralai/mistral-large", "name": "Mistral Large"},
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        },
    )


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(_catalog_handler),
        base_url="https://openrouter.ai/api/v1",
    )


@pytest.mark.asyncio
async def test_complete_rejects_model_not_enabled(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])
    async with _http_client() as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        with pytest.raises(ModelNotAllowedError) as exc_info:
            await client.complete(
                model="mistralai/mistral-large",
                messages=[{"role": "user", "content": "hi"}],
            )
    assert "Settings > Models" in str(exc_info.value)


@pytest.mark.asyncio
async def test_complete_allows_enabled_model(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])
    async with _http_client() as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        result = await client.complete(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_complete_rejects_everything_when_registry_empty(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, [])
    async with _http_client() as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        with pytest.raises(ModelNotAllowedError):
            await client.complete(
                model="openai/gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )


@pytest.mark.asyncio
async def test_list_models_is_unfiltered(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.model_allowlist = ["openai/gpt-4o"]
    registry = _make_registry(db, ["openai/gpt-4o"])
    async with _http_client() as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        models = await client.list_models()
    assert [m.id for m in models] == [
        "anthropic/claude-3.7-sonnet",
        "openai/gpt-4o",
        "mistralai/mistral-large",
    ]


@pytest.mark.asyncio
async def test_complete_checks_privacy_consent_before_model_enablement(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = False
    registry = _make_registry(db, ["openai/gpt-4o"])
    async with _http_client() as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        with pytest.raises(PrivacyConsentRequiredError):
            await client.complete(
                model="mistralai/mistral-large",
                messages=[{"role": "user", "content": "hi"}],
            )


@pytest.mark.asyncio
async def test_rejected_model_writes_no_spend_ledger_row(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])
    completion_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/models"):
            completion_requests.append(request)
        return _catalog_handler(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        with pytest.raises(ModelNotAllowedError):
            await client.complete(
                model="mistralai/mistral-large",
                messages=[{"role": "user", "content": "hi"}],
            )
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM spend_ledger;").fetchone()[0] == 0
    assert completion_requests == []


@pytest.mark.asyncio
async def test_list_models_is_unfiltered_when_catalog_unavailable(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.model_allowlist = ["openai/gpt-4o"]
    registry = _make_registry(db, ["openai/gpt-4o"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        models = await client.list_models()
    assert [m.id for m in models] == [m.id for m in DEFAULT_FALLBACK_MODELS]


@pytest.mark.asyncio
async def test_search_catalog_matches_id_and_name_case_insensitive(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    registry = _make_registry(db, [])
    async with _http_client() as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        by_id = await client.search_catalog("MISTRALAI/")
        by_name = await client.search_catalog("gpt-4O")
        none = await client.search_catalog("no-such-model")
    assert [m.id for m in by_id] == ["mistralai/mistral-large"]
    assert [m.id for m in by_name] == ["openai/gpt-4o"]
    assert none == []


@pytest.mark.asyncio
async def test_search_catalog_respects_limit(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    registry = _make_registry(db, [])
    async with _http_client() as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        limited = await client.search_catalog("", limit=2)
        matched = await client.search_catalog("o", limit=1)
    assert [m.id for m in limited] == ["anthropic/claude-3.7-sonnet", "openai/gpt-4o"]
    assert len(matched) == 1


@pytest.mark.asyncio
async def test_complete_releases_spend_on_cancellation(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])

    started = asyncio.Event()
    never_set = asyncio.Event()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await never_set.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(slow_handler),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        task = asyncio.create_task(
            client.complete(
                model="openai/gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert client.get_daily_spend_usd() == 0.0
    with db.get_connection() as conn:
        rows = conn.execute("SELECT status FROM spend_ledger;").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "released"


@pytest.mark.asyncio
async def test_complete_releases_spend_on_http_error(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="server error")),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        with pytest.raises(OpenRouterError):
            await client.complete(
                model="openai/gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
            )

    assert client.get_daily_spend_usd() == 0.0
    with db.get_connection() as conn:
        rows = conn.execute("SELECT status FROM spend_ledger;").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "released"


@pytest.mark.asyncio
async def test_complete_sends_session_id_bounded_to_256_chars(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])
    seen_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        await client.complete(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            session_id="proj:chat_123",
        )
        await client.complete(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            session_id="x" * 300,
        )
        await client.complete(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            session_id="   ",
        )

    assert seen_payloads[0]["session_id"] == "proj:chat_123"
    assert len(seen_payloads[1]["session_id"]) == 256
    assert "session_id" not in seen_payloads[2]


@pytest.mark.asyncio
async def test_local_model_complete_bypasses_consent_key_zdr_and_spend_ledger(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = False
    settings.daily_spend_cap_usd = 0.0
    secrets.update_secrets({"openrouter_api_key": ""})
    registry = ModelRegistry(db)
    registry.enable(
        EnabledModel(
            id="llama3.2:latest",
            name="llama3.2:latest",
            context_length=32768,
            prompt_price_per_token=0.0,
            completion_price_per_token=0.0,
            supports_vision=False,
            verified=True,
            provider="local",
            base_url="http://localhost:11434/v1",
        )
    )

    seen_requests: list[httpx.Request] = []
    seen_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        seen_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "local reply"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        result = await client.complete(
            model="llama3.2:latest",
            messages=[{"role": "user", "content": "hello local"}],
            session_id="proj:chat_1",
        )

    assert result.content == "local reply"
    assert result.cost_usd == 0.0
    assert result.reservation_id == ""
    assert len(seen_requests) == 1
    assert str(seen_requests[0].url) == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in {k.lower() for k in seen_requests[0].headers}
    assert "provider" not in seen_payloads[0]
    assert "session_id" not in seen_payloads[0]

    with db.get_connection() as conn:
        ledger_count = conn.execute("SELECT COUNT(*) FROM spend_ledger;").fetchone()[0]
    assert ledger_count == 0


@pytest.mark.asyncio
async def test_list_local_models_parses_openai_catalog_and_handles_unreachable(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    registry = _make_registry(db, [])

    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "llama3.2:latest"},
                    {"id": "llava:13b", "context_length": 8192},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        models, reachable = await client.list_local_models("http://localhost:11434/v1/")

    assert reachable is True
    assert [m.id for m in models] == ["llama3.2:latest", "llava:13b"]
    assert models[0].supports_vision is False
    assert models[1].supports_vision is True
    assert models[1].context_length == 8192
    assert models[0].prompt_price_per_token == 0.0

    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler)) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        models_down, reachable_down = await client.list_local_models("http://localhost:11434/v1")

    assert reachable_down is False
    assert models_down == []


@pytest.mark.asyncio
async def test_complete_captures_finish_reason(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["anthropic/claude-3.7-sonnet"])

    def length_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(length_handler),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        res = await client.complete(
            model="anthropic/claude-3.7-sonnet",
            messages=[{"role": "user", "content": "Hello"}],
        )

    assert res.content == "partial"
    assert res.finish_reason == "length"


@pytest.mark.asyncio
async def test_complete_rejects_error_payload_empty_choices_and_invalid_json(
    tmp_path: Path,
) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])

    payloads: list[httpx.Response] = [
        httpx.Response(200, json={"error": {"message": "Provider overloaded"}}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}, "finish_reason": "error"}]},
        ),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}, "finish_reason": "error"}]},
        ),
        httpx.Response(200, content=b"<html>502 Bad Gateway</html>"),
    ]
    idx = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal idx
        resp = payloads[idx]
        idx += 1
        return resp

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        for _ in range(4):
            with pytest.raises(OpenRouterError):
                await client.complete(
                    model="openai/gpt-4o",
                    messages=[{"role": "user", "content": "Hello"}],
                )

    assert client.get_daily_spend_usd() == 0.0


@pytest.mark.asyncio
async def test_complete_retries_once_on_finish_reason_error(tmp_path: Path) -> None:
    settings, db, secrets = _make_environment(tmp_path)
    settings.privacy_consent_accepted = True
    registry = _make_registry(db, ["openai/gpt-4o"])

    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": ""}, "finish_reason": "error"}]},
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Recovered reply."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    ) as http_client:
        client = OpenRouterClient(
            db=db, secrets=secrets, settings=settings, registry=registry, http_client=http_client
        )
        res = await client.complete(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "Hello"}],
        )
        assert res.content == "Recovered reply."
        assert attempts == 2

    assert client.get_daily_spend_usd() > 0.0




