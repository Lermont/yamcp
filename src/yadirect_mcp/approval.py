"""Одноразовые серверные подтверждения, привязанные к хешу плана."""

from __future__ import annotations

import json
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass

from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_TTL_SECONDS = 30 * 60
MAX_GRANTS = 1000


@dataclass(frozen=True)
class Grant:
    client_login: str
    plan_hash: str
    token: str
    expires_at: float
    evidence: dict | None = None

    @property
    def phrase(self) -> str:
        return (
            f"APPLY DIRECT PLAN {self.client_login} "
            f"{self.plan_hash[:12]} {self.token}"
        )


class ApprovalRegistry:
    """Хранилище preview-грантов в памяти одного MCP-процесса."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._grants: dict[str, Grant] = {}
        self._pending: dict[str, dict] = {}

    def issue(self, client_login: str, plan_hash: str, *, evidence: dict | None = None) -> Grant:
        self._prune()
        if len(self._grants) >= MAX_GRANTS:
            self._grants.pop(next(iter(self._grants)))
        token = secrets.token_urlsafe(18)
        grant = Grant(
            client_login=client_login,
            plan_hash=plan_hash,
            token=token,
            expires_at=time.monotonic() + self.ttl_seconds,
            evidence=deepcopy(evidence),
        )
        self._grants[token] = grant
        return grant

    def _prune(self) -> None:
        now = time.monotonic()
        self._grants = {
            token: grant for token, grant in self._grants.items() if grant.expires_at > now
        }
        self._pending = {
            token: pending for token, pending in self._pending.items() if token in self._grants
        }

    def validate(self, confirmation: str, client_login: str, plan_hash: str) -> Grant:
        """Check before asking the host, without burning a grant on a cancelled prompt."""
        token = confirmation.rsplit(" ", 1)[-1] if confirmation else ""
        grant = self._grants.get(token)
        self._prune()
        if grant is None:
            raise ValueError("Подтверждение неизвестно, уже использовано или сервер перезапущен")
        if time.monotonic() >= grant.expires_at:
            raise ValueError("Подтверждение истекло; сформируйте новый preview")
        if grant.client_login != client_login or grant.plan_hash != plan_hash:
            raise ValueError("Подтверждение относится к другому логину или версии плана")
        if confirmation != grant.phrase:
            raise ValueError("Фраза подтверждения не совпадает с preview")
        return grant

    def consume(self, confirmation: str, client_login: str, plan_hash: str) -> Grant:
        grant = self.validate(confirmation, client_login, plan_hash)
        self._grants.pop(grant.token)
        return grant

    async def authorize(
        self, context, plan: dict, operation: str, confirmation: str, *, mode: str = "elicitation"
    ) -> dict:
        """Reserve across the UI await; recheck expiry and consume only after consent."""
        if mode not in {"elicitation", "task_authorized"}:
            raise ValueError("Неизвестный режим согласования записи")
        grant = self.validate(confirmation, plan["client_login"], plan["plan_hash"])
        version = getattr(context, "protocol_version", None) or ""
        if mode == "elicitation" and version >= "2026-07-28":
            return self._authorize_round(context, plan, operation, confirmation, grant)
        if grant.token in self._pending:
            raise ValueError("Для этого подтверждения уже открыт запрос согласия")
        self._pending[grant.token] = {}
        try:
            receipt = (task_receipt(plan, operation) if mode == "task_authorized"
                       else await elicit(context, plan, operation))
            self.consume(confirmation, plan["client_login"], plan["plan_hash"])
            return receipt
        finally:
            self._pending.pop(grant.token, None)

    def _authorize_round(self, context, plan, operation, confirmation, grant) -> dict:
        """Manual SDK multi-round-trip flow, at the existing pre-write boundary.

        MCPServer seals state and binds it to tool arguments. The registry also
        binds the exact question, client assertion and a fresh reservation nonce.
        No job starts and no token is consumed on the input-required round.
        An abandoned round holds the token until its original preview TTL expires.
        """
        client = require_client(context)
        state = context.request_state
        responses = context.input_responses
        message = consent_message(plan, operation)
        binding = {"client": client, "client_login": plan["client_login"],
                   "plan_hash": plan["plan_hash"], "operation": operation, "message": message}
        if state is None:
            if responses:
                raise ValueError("Ответ подтверждения без состояния запроса")
            if grant.token in self._pending:
                raise ValueError("Для этого подтверждения уже открыт запрос согласия")
            pending = {**binding, "nonce": secrets.token_urlsafe(24)}
            self._pending[grant.token] = pending
            raise InputRequired(InputRequiredResult(
                request_state=json.dumps(pending, ensure_ascii=False, sort_keys=True),
                input_requests={"consent": ElicitRequest(params=ElicitRequestFormParams(
                    message=message, requested_schema=Consent.model_json_schema(),
                ))},
            ))
        pending = self._pending.get(grant.token)
        # This is already unsealed by MCPServer; never accept an old response
        # after decline/cancel, a new prompt, token use, or process restart.
        if (pending is None or json.loads(state) != pending
                or any(pending.get(key) != value for key, value in binding.items())):
            raise ValueError("Состояние согласования не совпадает с резервированием")
        try:
            result = (responses.get("consent")
                      if responses and set(responses) == {"consent"} else None)
            if (not isinstance(result, ElicitResult)
                    or (result.action == "accept" and result.content is None)):
                raise ConsentError("mcp_elicitation_invalid_response",
                                   "Некорректный ответ подтверждения MCP. Запись не выполнялась.",
                                   client=client)
            receipt = consent_receipt(
                result.action, (result.content or {}).get("approve"), client, plan, operation,
            )
            self.consume(confirmation, plan["client_login"], plan["plan_hash"])
            return receipt
        finally:
            self._pending.pop(grant.token, None)


REGISTRY = ApprovalRegistry()


class InputRequired(Exception):
    """Control flow returned by the tool boundary, never logged as a failure."""

    def __init__(self, result: InputRequiredResult):
        super().__init__("MCP input required")
        self.result = result


class ConsentError(PermissionError):
    """Machine-readable host outcome; never an authorization to fall back to raw writes."""

    def __init__(self, code: str, message: str, *, client: dict, action: str | None = None):
        super().__init__(message)
        self.code = code
        self.client = client
        self.action = action

    def as_dict(self) -> dict:
        return {"error_code": self.code, "approval": {
            "mechanism": "mcp_elicitation", "action": self.action,
            "client": self.client, "authorized": False,
        }, "executed": False}


def client_info(context) -> dict:
    session = getattr(context, "session", None)
    params = getattr(session, "client_params", None)
    client = getattr(params, "client_info", None)
    capabilities = getattr(session, "client_capabilities", None)
    elicitation = getattr(capabilities, "elicitation", None)
    supports_form = False
    if capabilities is not None:
        # An empty legacy capability means form support, but URL-only does not.
        supports_form = elicitation is not None and (
            getattr(elicitation, "form", None) is not None
            or not elicitation.model_dump(exclude_none=True)
        )
    return {"name": getattr(client, "name", None),
            "version": getattr(client, "version", None), "supports_form": supports_form}


def require_client(context) -> dict:
    client = client_info(context)
    if client["supports_form"] is not True:
        raise ConsentError(
            "mcp_elicitation_unsupported",
            "Клиент не поддерживает форму подтверждения MCP. Запись не выполнялась. "
            "Подключите клиент с поддержкой form elicitation.", client=client,
        )
    if not client["name"] or not client["version"]:
        raise ConsentError("mcp_elicitation_unknown_client",
                           "Неизвестна идентичность MCP-клиента. Запись не выполнялась.",
                           client=client)
    return client


def _form_schema_extra(schema: dict) -> None:
    # Codex 0.154's typed form parser rejects Pydantic's optional root title.
    # Keep property labels, the required boolean, and normal response validation.
    schema.pop("title", None)


class Consent(BaseModel):
    model_config = ConfigDict(json_schema_extra=_form_schema_extra, strict=True)

    approve: bool = Field(description="Подтверждаю запись указанного неизменного плана")


def consent_message(plan: dict, operation: str) -> str:
    return (f"Подтвердите {operation} для {plan['client_login']}. "
            f"Полный хеш: {plan['plan_hash']}. Сводка: {plan.get('summary', {})}. "
            + ("Показы могут начаться после допуска модерацией. "
               if plan.get("schema") == "direct_ad_resume_v1" else "Показы не запускаются. ")
            + "Подтверждение относится только к этому хешу.")


async def elicit(context, plan: dict, operation: str) -> dict:
    client = require_client(context)
    result = await context.elicit(
        message=consent_message(plan, operation), schema=Consent,
    )
    if result.action == "accept" and getattr(result, "data", None) is None:
        raise ConsentError("mcp_elicitation_invalid_response",
                           "Некорректный ответ подтверждения MCP. Запись не выполнялась.",
                           client=client, action=result.action)
    return consent_receipt(result.action, getattr(getattr(result, "data", None), "approve", None),
                           client, plan, operation)


def consent_receipt(action, approved, client: dict, plan: dict, operation: str) -> dict:
    from datetime import UTC, datetime

    if action in {"decline", "cancel"}:
        raise ConsentError(
            "mcp_elicitation_" + action,
            "MCP-клиент вернул " + action + ". Запись не выполнялась. "
            "Это может быть ответ пользователя или автоматический ответ клиента. "
            "Если форма не появилась в Codex, проверьте политику разрешений: "
            "never или отключённый mcp_elicitations не позволяют показать запрос. "
            "В интерактивном режиме проверьте журнал клиента на ошибки схемы формы. "
            "После изменения политики нужен повторный вызов; отказ не является согласием.",
            client=client, action=action,
        )
    if action != "accept":
        raise ConsentError(
            "mcp_elicitation_invalid_response", "Некорректный ответ подтверждения MCP. "
            "Запись не выполнялась.", client=client, action=action,
        )
    if approved is not True:
        raise ConsentError(
            "mcp_elicitation_not_approved", "Форма принята, но согласие approve=true "
            "не получено. Запись не выполнялась.", client=client, action=action,
        )
    # Client identity is an assertion of the connected host, not an authenticated person.
    return {"mechanism": "mcp_elicitation", "decision": "accept",
            "client_login": plan["client_login"], "plan_hash": plan["plan_hash"],
            "operation": operation, "at": datetime.now(UTC).isoformat(),
            "actor": client["name"] or "connected_mcp_client",
            "identity_assurance": "client_attested"}


def task_receipt(plan: dict, operation: str) -> dict:
    """Host-configured delegation, not a fabricated human elicitation response."""
    from datetime import UTC, datetime

    return {"mechanism": "configured_task_authorization", "decision": "accept",
            "client_login": plan["client_login"], "plan_hash": plan["plan_hash"],
            "operation": operation, "at": datetime.now(UTC).isoformat(),
            "actor": "trusted_mcp_client", "identity_assurance": "task_scope_delegated"}
