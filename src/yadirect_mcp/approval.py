"""Одноразовые серверные подтверждения, привязанные к хешу плана."""

from __future__ import annotations

import secrets
import time
from copy import deepcopy
from dataclasses import dataclass

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
        self._pending: set[str] = set()

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

    async def authorize(self, context, plan: dict, operation: str, confirmation: str) -> dict:
        """Reserve across the UI await; recheck expiry and consume only after consent."""
        grant = self.validate(confirmation, plan["client_login"], plan["plan_hash"])
        if grant.token in self._pending:
            raise ValueError("Для этого подтверждения уже открыт запрос согласия")
        self._pending.add(grant.token)
        try:
            receipt = await elicit(context, plan, operation)
            self.consume(confirmation, plan["client_login"], plan["plan_hash"])
            return receipt
        finally:
            self._pending.discard(grant.token)


REGISTRY = ApprovalRegistry()


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
    client = getattr(params, "clientInfo", None)
    capabilities = getattr(params, "capabilities", None)
    elicitation = getattr(capabilities, "elicitation", None)
    supports_form = None
    if capabilities is not None:
        # An empty legacy capability means form support, but URL-only does not.
        supports_form = elicitation is not None and (
            getattr(elicitation, "form", None) is not None
            or not elicitation.model_dump(exclude_none=True)
        )
    return {"name": getattr(client, "name", None),
            "version": getattr(client, "version", None), "supports_form": supports_form}


def _form_schema_extra(schema: dict) -> None:
    # Codex 0.154's typed form parser rejects Pydantic's optional root title.
    # Keep property labels, the required boolean, and normal response validation.
    schema.pop("title", None)


async def elicit(context, plan: dict, operation: str) -> dict:
    from datetime import UTC, datetime

    from pydantic import BaseModel, ConfigDict, Field

    class Consent(BaseModel):
        model_config = ConfigDict(json_schema_extra=_form_schema_extra)

        approve: bool = Field(description="Подтверждаю запись указанного неизменного плана")

    client = client_info(context)
    if context is None or client["supports_form"] is False:
        raise ConsentError(
            "mcp_elicitation_unsupported",
            "Клиент не поддерживает форму подтверждения MCP. Запись не выполнялась. "
            "Подключите клиент с поддержкой form elicitation.", client=client,
        )
    result = await context.elicit(
        message=(f"Подтвердите {operation} для {plan['client_login']}. "
                 f"Полный хеш: {plan['plan_hash']}. Сводка: {plan.get('summary', {})}. "
                 "Показы не запускаются. Подтверждение относится только к этому хешу."),
        schema=Consent,
    )
    if result.action in {"decline", "cancel"}:
        raise ConsentError(
            "mcp_elicitation_" + result.action,
            "MCP-клиент вернул " + result.action + ". Запись не выполнялась. "
            "Это может быть ответ пользователя или автоматический ответ клиента. "
            "Если форма не появилась в Codex, проверьте политику разрешений: "
            "never или отключённый mcp_elicitations не позволяют показать запрос. "
            "В интерактивном режиме проверьте журнал клиента на ошибки схемы формы. "
            "После изменения политики нужен повторный вызов; отказ не является согласием.",
            client=client, action=result.action,
        )
    if result.action != "accept" or result.data is None:
        raise ConsentError(
            "mcp_elicitation_invalid_response", "Некорректный ответ подтверждения MCP. "
            "Запись не выполнялась.", client=client, action=result.action,
        )
    if getattr(result.data, "approve", None) is not True:
        raise ConsentError(
            "mcp_elicitation_not_approved", "Форма принята, но согласие approve=true "
            "не получено. Запись не выполнялась.", client=client, action=result.action,
        )
    # Client identity is an assertion of the connected host, not an authenticated person.
    return {"mechanism": "mcp_elicitation", "decision": "accept",
            "client_login": plan["client_login"], "plan_hash": plan["plan_hash"],
            "operation": operation, "at": datetime.now(UTC).isoformat(),
            "actor": client["name"] or "connected_mcp_client",
            "identity_assurance": "client_attested"}
