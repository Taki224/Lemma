from __future__ import annotations

import asyncio
import base64
import logging
import secrets as pysecrets
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Any

import httpx

from app.ai.model_registry import ModelRegistry
from app.config import AppSettings
from app.config_secrets import SecretsStore
from app.db import DatabaseManager

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class PrivacyConsentRequiredError(PermissionError):
    """Raised when an AI model call is attempted before first-run privacy consent is accepted."""


class ModelNotAllowedError(PermissionError):
    """Raised when a requested model ID is not enabled in Settings > Models."""


class SpendCapExceededError(RuntimeError):
    """Raised when an AI request exceeds REQUEST_SPEND_CAP_USD or DAILY_SPEND_CAP_USD."""


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter API fails or API key is missing."""


class OpenRouterFinishError(OpenRouterError):
    """Raised when OpenRouter terminates a completion with finish_reason='error'."""


@dataclass(frozen=True)
class OpenRouterModel:
    id: str
    name: str
    context_length: int = 128000
    prompt_price_per_token: float = 0.000003
    completion_price_per_token: float = 0.000015
    supports_vision: bool = False


@dataclass(frozen=True)
class CompletionResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    reservation_id: str
    finish_reason: str | None = None


@dataclass(frozen=True)
class ApplyPatchOperation:
    call_id: str
    op_type: str
    path: str
    diff: str


@dataclass(frozen=True)
class ApplyPatchResult:
    content: str
    operations: list[ApplyPatchOperation]
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    finish_reason: str | None = None


DEFAULT_FALLBACK_MODELS: list[OpenRouterModel] = [
    OpenRouterModel(
        id="anthropic/claude-3.7-sonnet",
        name="Claude 3.7 Sonnet",
        context_length=200000,
        prompt_price_per_token=0.000003,
        completion_price_per_token=0.000015,
        supports_vision=True,
    ),
    OpenRouterModel(
        id="openai/gpt-4o",
        name="GPT-4o",
        context_length=128000,
        prompt_price_per_token=0.0000025,
        completion_price_per_token=0.000010,
        supports_vision=True,
    ),
    OpenRouterModel(
        id="google/gemini-2.5-pro",
        name="Gemini 2.5 Pro",
        context_length=1000000,
        prompt_price_per_token=0.00000125,
        completion_price_per_token=0.000010,
        supports_vision=True,
    ),
]


def _estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    vision_images: list[bytes] | None = None,
) -> int:
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_chars += len(str(part.get("text", "")))
    text_tokens = max(1, total_chars // 4)
    image_tokens = 1000 * len(vision_images) if vision_images else 0
    return text_tokens + image_tokens


def _detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    return "image/png"


class OpenRouterClient:
    """Unified OpenRouter client enforcing privacy consent, enabled models, ZDR, and spend caps."""

    _spend_lock = threading.Lock()

    def __init__(
        self,
        db: DatabaseManager,
        secrets: SecretsStore,
        settings: AppSettings,
        registry: ModelRegistry,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = db
        self.secrets = secrets
        self.settings = settings
        self.registry = registry
        self._http_client = http_client
        self._cached_models: list[OpenRouterModel] | None = None
        self.catalog_is_live = True
        self._pricing_map: dict[str, tuple[float, float]] = {
            m.id: (m.prompt_price_per_token, m.completion_price_per_token)
            for m in DEFAULT_FALLBACK_MODELS
        }

    def set_model_pricing(
        self,
        model: str,
        prompt_price_per_token: float,
        completion_price_per_token: float,
    ) -> None:
        self._pricing_map[model] = (
            float(prompt_price_per_token),
            float(completion_price_per_token),
        )

    async def list_models(self, force_refresh: bool = False) -> list[OpenRouterModel]:
        if self._cached_models is not None and not force_refresh:
            return list(self._cached_models)

        api_key = self.secrets.get("openrouter_api_key")
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            if self._http_client is not None:
                resp = await self._http_client.get("/models", headers=headers)
            else:
                async with httpx.AsyncClient(
                    base_url=OPENROUTER_BASE_URL, timeout=15.0
                ) as client:
                    resp = await client.get("/models", headers=headers)

            if resp.status_code == 200:
                payload = resp.json()
                raw_items = payload.get("data", [])
                parsed: list[OpenRouterModel] = []
                for item in raw_items:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    mid = str(item["id"])
                    name = str(item.get("name") or mid)
                    ctx_len = int(item.get("context_length") or 128000)
                    pricing = item.get("pricing") or {}
                    try:
                        raw_p = pricing.get("prompt")
                        p_rate = max(0.0, float(raw_p)) if raw_p is not None else 0.000003
                    except (TypeError, ValueError):
                        p_rate = 0.000003
                    try:
                        raw_c = pricing.get("completion")
                        c_rate = max(0.0, float(raw_c)) if raw_c is not None else 0.000015
                    except (TypeError, ValueError):
                        c_rate = 0.000015
                    arch = item.get("architecture") or {}
                    modality = str(arch.get("modality") or "")
                    supports_vision = "image" in modality.lower() or any(
                        v in mid.lower() for v in ("gpt-4o", "claude-3", "gemini", "vision", "vl")
                    )
                    model_obj = OpenRouterModel(
                        id=mid,
                        name=name,
                        context_length=ctx_len,
                        prompt_price_per_token=p_rate,
                        completion_price_per_token=c_rate,
                        supports_vision=supports_vision,
                    )
                    parsed.append(model_obj)
                    self._pricing_map[mid] = (p_rate, c_rate)
                if parsed:
                    self._cached_models = parsed
                    self.catalog_is_live = True
                    return list(parsed)
        except Exception:
            pass

        self._cached_models = list(DEFAULT_FALLBACK_MODELS)
        self.catalog_is_live = False
        return list(self._cached_models)

    async def search_catalog(
        self, query: str, limit: int = 25
    ) -> list[OpenRouterModel]:
        needle = query.strip().lower()
        catalog = await self.list_models()
        matches = [
            m
            for m in catalog
            if not needle or needle in m.id.lower() or needle in m.name.lower()
        ]
        return matches[: max(0, limit)]

    async def list_local_models(
        self, base_url: str
    ) -> tuple[list[OpenRouterModel], bool]:
        """Query an OpenAI-compatible local server's /models endpoint."""
        target_url = base_url.strip().rstrip("/")
        if not target_url:
            return [], False
        headers: dict[str, str] = {}
        local_api_key = self.secrets.get("local_api_key")
        if local_api_key:
            headers["Authorization"] = f"Bearer {local_api_key}"

        try:
            if self._http_client is not None:
                resp = await self._http_client.get(f"{target_url}/models", headers=headers)
            else:
                async with httpx.AsyncClient(
                    base_url=target_url, timeout=10.0
                ) as client:
                    resp = await client.get("/models", headers=headers)

            if resp.status_code == 200:
                payload = resp.json()
                raw_items = payload.get("data", [])
                parsed: list[OpenRouterModel] = []
                for item in raw_items:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    mid = str(item["id"])
                    name = str(item.get("name") or mid)
                    ctx_len = int(item.get("context_length") or 32768)
                    supports_vision = any(
                        v in mid.lower()
                        for v in (
                            "vision",
                            "vl",
                            "llava",
                            "minicpm-v",
                            "gemma3",
                            "pixtral",
                            "moondream",
                        )
                    )
                    parsed.append(
                        OpenRouterModel(
                            id=mid,
                            name=name,
                            context_length=ctx_len,
                            prompt_price_per_token=0.0,
                            completion_price_per_token=0.0,
                            supports_vision=supports_vision,
                        )
                    )
                return parsed, True
        except Exception:
            pass
        return [], False

    def estimate_cost_usd(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        p_rate, c_rate = self._pricing_map.get(model, (0.000003, 0.000015))
        cost = max(0, prompt_tokens) * p_rate + max(0, completion_tokens) * c_rate
        return round(max(0.0, cost), 8)

    def get_daily_spend_usd(self) -> float:
        cutoff = int(time.time()) - 86400
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0.0) AS total_spend
                FROM spend_ledger
                WHERE created_at >= ? AND status IN ('reserved', 'completed');
                """,
                (cutoff,),
            ).fetchone()
            return float(row["total_spend"]) if row is not None else 0.0

    def reserve_spend(self, model: str, estimated_usd: float) -> str:
        """Atomically verify request and 24h daily spend caps and insert a 'reserved' ledger row."""
        est = max(0.0, float(estimated_usd))
        if est > self.settings.request_spend_cap_usd:
            raise SpendCapExceededError(
                f"Estimated request cost ${est:.4f} exceeds REQUEST_SPEND_CAP_USD "
                f"(${self.settings.request_spend_cap_usd:.2f})."
            )

        reservation_id = f"res_{pysecrets.token_hex(12)}"
        now_ts = int(time.time())
        cutoff = now_ts - 86400

        with self._spend_lock:
            with closing(self.db.get_connection()) as conn:
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    row = conn.execute(
                        """
                        SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0.0) AS total_spend
                        FROM spend_ledger
                        WHERE created_at >= ? AND status IN ('reserved', 'completed');
                        """,
                        (cutoff,),
                    ).fetchone()
                    current_daily = float(row["total_spend"]) if row is not None else 0.0
                    if current_daily + est > self.settings.daily_spend_cap_usd + 1e-9:
                        raise SpendCapExceededError(
                            f"Cumulative 24h spend (${current_daily:.4f} + ${est:.4f}) would exceed "
                            f"DAILY_SPEND_CAP_USD (${self.settings.daily_spend_cap_usd:.2f})."
                        )
                    conn.execute(
                        """
                        INSERT INTO spend_ledger (
                            reservation_id, created_at, model, reserved_usd, actual_usd, status
                        ) VALUES (?, ?, ?, ?, NULL, 'reserved');
                        """,
                        (reservation_id, now_ts, model, est),
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise

        return reservation_id

    def reconcile_spend(self, reservation_id: str, actual_usd: float) -> None:
        actual = max(0.0, float(actual_usd))
        with self._spend_lock:
            with closing(self.db.get_connection()) as conn:
                conn.execute(
                    """
                    UPDATE spend_ledger
                    SET actual_usd = ?, status = 'completed'
                    WHERE reservation_id = ?;
                    """,
                    (actual, reservation_id),
                )

    def release_spend(self, reservation_id: str) -> None:
        with self._spend_lock:
            with closing(self.db.get_connection()) as conn:
                conn.execute(
                    """
                    UPDATE spend_ledger
                    SET actual_usd = 0.0, status = 'released'
                    WHERE reservation_id = ? AND status = 'reserved';
                    """,
                    (reservation_id,),
                )

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        vision_images: list[bytes] | None = None,
        max_tokens: int = 4096,
        extra_body: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> CompletionResult:
        enabled_model = self.registry.get_model(model)
        is_local = enabled_model is not None and enabled_model.provider == "local"

        if not is_local and not self.settings.privacy_consent_accepted:
            raise PrivacyConsentRequiredError(
                "Manuscript privacy consent must be accepted before calling OpenRouter models."
            )

        if enabled_model is None:
            raise ModelNotAllowedError(
                f"Model {model!r} is not enabled. Enable it in Settings > Models."
            )

        bounded_max_tokens = max(1, min(int(max_tokens), 32768))
        est_prompt_tokens = _estimate_prompt_tokens(messages, vision_images)

        if is_local:
            return await self._complete_local(
                model=model,
                base_url=enabled_model.base_url or self.settings.local_model_base_url,
                messages=messages,
                vision_images=vision_images,
                max_tokens=bounded_max_tokens,
                est_prompt_tokens=est_prompt_tokens,
                extra_body=extra_body,
            )

        estimated_usd = self.estimate_cost_usd(
            model=model,
            prompt_tokens=est_prompt_tokens,
            completion_tokens=bounded_max_tokens,
        )

        reservation_id = self.reserve_spend(model=model, estimated_usd=estimated_usd)

        try:
            formatted_messages = _format_messages_with_vision(messages, vision_images)

            payload: dict[str, Any] = {}
            if extra_body:
                payload.update(extra_body)
            payload["model"] = model
            payload["messages"] = formatted_messages
            payload["max_tokens"] = bounded_max_tokens
            if session_id and session_id.strip():
                payload["session_id"] = session_id.strip()[:256]

            # Non-overridable ZDR provider routing unless explicitly disabled in settings.yaml
            if getattr(self.settings, "zdr_enabled", True):
                payload["provider"] = {
                    "data_collection": "deny",
                    "zdr": True,
                }

            api_key = self.secrets.get("openrouter_api_key")
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            elif self._http_client is None:
                raise OpenRouterError(
                    "OpenRouter API key is not configured in /data/config/secrets.yaml."
                )

            for attempt in range(2):
                try:
                    if self._http_client is not None:
                        resp = await self._http_client.post(
                            "/chat/completions",
                            json=payload,
                            headers=headers,
                        )
                    else:
                        async with httpx.AsyncClient(
                            base_url=OPENROUTER_BASE_URL, timeout=120.0
                        ) as client:
                            resp = await client.post(
                                "/chat/completions",
                                json=payload,
                                headers=headers,
                            )
                except httpx.HTTPError as exc:
                    raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

                if resp.status_code >= 400:
                    raise OpenRouterError(
                        f"OpenRouter completion failed with status {resp.status_code}: {resp.text}"
                    )

                try:
                    data = resp.json()
                except ValueError as exc:
                    raise OpenRouterError("OpenRouter returned invalid JSON.") from exc
                try:
                    content_text, actual_prompt, actual_completion, usage, finish_reason = (
                        _parse_completion_payload(data, est_prompt_tokens)
                    )
                    break
                except OpenRouterFinishError:
                    if attempt == 0:
                        logger.warning(
                            "Retrying OpenRouter completion for model=%s after finish_reason='error'",
                            model,
                        )
                        await asyncio.sleep(0.25)
                        continue
                    raise
            if "cost" in usage or "total_cost" in usage:
                actual_usd = float(usage.get("cost") or usage.get("total_cost") or 0.0)
            else:
                actual_usd = self.estimate_cost_usd(
                    model=model,
                    prompt_tokens=actual_prompt,
                    completion_tokens=actual_completion,
                )

            self.reconcile_spend(reservation_id, actual_usd)
            return CompletionResult(
                content=content_text,
                model=model,
                prompt_tokens=actual_prompt,
                completion_tokens=actual_completion,
                cost_usd=actual_usd,
                reservation_id=reservation_id,
                finish_reason=finish_reason,
            )
        except BaseException:
            self.release_spend(reservation_id)
            raise

    async def apply_patch(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> ApplyPatchResult:
        """Ask the Responses API for an apply_patch tool call. Does not write files."""
        enabled_model = self.registry.get_model(model)
        is_local = enabled_model is not None and enabled_model.provider == "local"
        if is_local:
            raise OpenRouterError("Apply patch is only available for OpenRouter models.")
        if not self.settings.privacy_consent_accepted:
            raise PrivacyConsentRequiredError(
                "Manuscript privacy consent must be accepted before calling OpenRouter models."
            )
        if enabled_model is None:
            raise ModelNotAllowedError(
                f"Model {model!r} is not enabled. Enable it in Settings > Models."
            )

        bounded_max_tokens = max(1, min(int(max_tokens), 32768))
        est_prompt_tokens = _estimate_prompt_tokens(messages)
        estimated_usd = self.estimate_cost_usd(
            model=model,
            prompt_tokens=est_prompt_tokens,
            completion_tokens=bounded_max_tokens,
        )
        reservation_id = self.reserve_spend(model=model, estimated_usd=estimated_usd)
        try:
            instructions, input_items = _messages_to_responses_input(messages)
            payload: dict[str, Any] = {
                "model": model,
                "instructions": instructions,
                "input": input_items,
                "max_output_tokens": bounded_max_tokens,
                # HITL returns one complete apply_patch_call. Native streaming
                # is OpenAI-only and this client does not consume diff deltas.
                "tools": [
                    {
                        "type": "openrouter:apply_patch",
                        "parameters": {"engine": "openrouter"},
                    }
                ],
            }
            if getattr(self.settings, "zdr_enabled", True):
                payload["provider"] = {"data_collection": "deny", "zdr": True}

            api_key = self.secrets.get("openrouter_api_key")
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            elif self._http_client is None:
                raise OpenRouterError(
                    "OpenRouter API key is not configured in /data/config/secrets.yaml."
                )

            try:
                if self._http_client is not None:
                    resp = await self._http_client.post(
                        "/responses", json=payload, headers=headers
                    )
                else:
                    async with httpx.AsyncClient(
                        base_url=OPENROUTER_BASE_URL, timeout=120.0
                    ) as client:
                        resp = await client.post("/responses", json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

            if resp.status_code >= 400:
                raise OpenRouterError(
                    f"OpenRouter responses failed with status {resp.status_code}: {resp.text}"
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise OpenRouterError("OpenRouter returned invalid JSON.") from exc
            if not isinstance(data, dict):
                raise OpenRouterError("OpenRouter returned an unexpected responses payload.")
            if data.get("error") and not data.get("output"):
                raise OpenRouterError(f"OpenRouter responses failed: {data.get('error')}")

            content_text, operations, actual_prompt, actual_completion, usage, finish_reason = (
                _parse_responses_payload(data, est_prompt_tokens)
            )
            if "cost" in usage or "total_cost" in usage:
                actual_usd = float(usage.get("cost") or usage.get("total_cost") or 0.0)
            else:
                actual_usd = self.estimate_cost_usd(
                    model=model,
                    prompt_tokens=actual_prompt,
                    completion_tokens=actual_completion,
                )
            self.reconcile_spend(reservation_id, actual_usd)
            return ApplyPatchResult(
                content=content_text,
                operations=operations,
                model=model,
                prompt_tokens=actual_prompt,
                completion_tokens=actual_completion,
                cost_usd=actual_usd,
                finish_reason=finish_reason,
            )
        except BaseException:
            self.release_spend(reservation_id)
            raise

    async def _complete_local(
        self,
        *,
        model: str,
        base_url: str,
        messages: list[dict[str, Any]],
        vision_images: list[bytes] | None,
        max_tokens: int,
        est_prompt_tokens: int,
        extra_body: dict[str, Any] | None,
    ) -> CompletionResult:
        target_base_url = base_url.strip().rstrip("/")
        if not target_base_url:
            raise OpenRouterError("Local model base URL is not configured.")

        formatted_messages = _format_messages_with_vision(messages, vision_images)
        payload: dict[str, Any] = {}
        if extra_body:
            payload.update(extra_body)
        payload["model"] = model
        payload["messages"] = formatted_messages
        payload["max_tokens"] = max_tokens

        headers: dict[str, str] = {"Content-Type": "application/json"}
        local_api_key = self.secrets.get("local_api_key")
        if local_api_key:
            headers["Authorization"] = f"Bearer {local_api_key}"

        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    f"{target_base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(
                    base_url=target_base_url, timeout=120.0
                ) as client:
                    resp = await client.post(
                        "/chat/completions",
                        json=payload,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise OpenRouterError(
                f"Local model server error ({target_base_url}): {exc}"
            ) from exc

        if resp.status_code >= 400:
            raise OpenRouterError(
                f"Local model completion failed with status {resp.status_code}: {resp.text}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenRouterError(
                f"Local model server ({target_base_url}) returned invalid JSON."
            ) from exc
        content_text, actual_prompt, actual_completion, _, finish_reason = (
            _parse_completion_payload(data, est_prompt_tokens)
        )
        return CompletionResult(
            content=content_text,
            model=model,
            prompt_tokens=actual_prompt,
            completion_tokens=actual_completion,
            cost_usd=0.0,
            reservation_id="",
            finish_reason=finish_reason,
        )


def _format_messages_with_vision(
    messages: list[dict[str, Any]],
    vision_images: list[bytes] | None,
) -> list[dict[str, Any]]:
    formatted_messages = [dict(m) for m in messages]
    if not vision_images:
        return formatted_messages
    if not formatted_messages:
        formatted_messages.append({"role": "user", "content": ""})
    last_msg = dict(formatted_messages[-1])
    raw_content = last_msg.get("content", "")
    content_parts: list[dict[str, Any]] = []
    if isinstance(raw_content, str):
        if raw_content:
            content_parts.append({"type": "text", "text": raw_content})
    elif isinstance(raw_content, list):
        content_parts.extend(raw_content)
    for img_bytes in vision_images:
        mime = _detect_image_mime(img_bytes)
        b64 = base64.b64encode(img_bytes).decode("ascii")
        content_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )
    last_msg["content"] = content_parts
    formatted_messages[-1] = last_msg
    return formatted_messages


def _parse_completion_payload(
    data: Any, est_prompt_tokens: int
) -> tuple[str, int, int, dict[str, Any], str | None]:
    if not isinstance(data, dict):
        raise OpenRouterError("Model response payload must be a JSON object.")

    top_error = data.get("error")
    if top_error:
        err_msg = (
            str(top_error.get("message") or top_error)
            if isinstance(top_error, dict)
            else str(top_error)
        )
        raise OpenRouterError(f"Model completion failed: {err_msg}")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OpenRouterError("Model response contained no completion choices.")

    first_choice = choices[0]
    choice_error = first_choice.get("error")
    if choice_error:
        err_msg = (
            str(choice_error.get("message") or choice_error)
            if isinstance(choice_error, dict)
            else str(choice_error)
        )
        raise OpenRouterError(f"Model completion failed: {err_msg}")

    msg_obj = first_choice.get("message") or {}
    if not isinstance(msg_obj, dict):
        raise OpenRouterError("Model response choice had an invalid message object.")
    content_text = str(msg_obj.get("content") or "")
    finish_reason: str | None = None
    raw_reason = first_choice.get("finish_reason")
    if isinstance(raw_reason, str) and raw_reason.strip():
        finish_reason = raw_reason.strip().lower()
    if finish_reason == "error":
        logger.warning(
            "OpenRouter completion terminated with finish_reason='error' "
            "(id=%s, model=%s, provider=%s, native_finish_reason=%s)",
            data.get("id"),
            data.get("model"),
            data.get("provider"),
            first_choice.get("native_finish_reason"),
        )
        raise OpenRouterFinishError("Model completion terminated with finish_reason='error'.")

    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    actual_prompt = int(usage.get("prompt_tokens") or est_prompt_tokens)
    actual_completion = int(
        usage.get("completion_tokens") or max(1, len(content_text) // 4)
    )
    return content_text, actual_prompt, actual_completion, usage, finish_reason


def _messages_to_responses_input(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    instructions: list[str] = []
    items: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if role == "system":
            if text:
                instructions.append(text)
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        items.append({"role": role, "content": text})
    return "\n\n".join(instructions), items


def _response_text_parts(item: dict[str, Any]) -> list[str]:
    content = item.get("content")
    if isinstance(content, str):
        return [content] if content else []
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                if part:
                    parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("output_text") or ""
            if isinstance(text, str) and text:
                parts.append(text)
    return parts


def _parse_responses_payload(
    data: dict[str, Any], est_prompt_tokens: int
) -> tuple[str, list[ApplyPatchOperation], int, int, dict[str, Any], str | None]:
    texts: list[str] = []
    operations: list[ApplyPatchOperation] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"message", "output_message"}:
                texts.extend(_response_text_parts(item))
                continue
            if item_type != "apply_patch_call":
                continue
            operation = item.get("operation")
            if not isinstance(operation, dict):
                continue
            operations.append(
                ApplyPatchOperation(
                    call_id=str(item.get("call_id") or ""),
                    op_type=str(operation.get("type") or ""),
                    path=str(operation.get("path") or ""),
                    diff=str(operation.get("diff") or ""),
                )
            )
    if not texts and isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])

    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    content_text = "\n".join(part for part in texts if part).strip()
    actual_prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or est_prompt_tokens)
    actual_completion = int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or max(1, len(content_text) // 4)
    )
    status = data.get("status")
    finish_reason = status.strip().lower() if isinstance(status, str) and status.strip() else None
    return content_text, operations, actual_prompt, actual_completion, usage, finish_reason
