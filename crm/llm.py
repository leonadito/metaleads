"""Calls to the LLM through the Agitare Core's gateway (Phase 7). The CRM
never holds a provider key: it sends a Messages API request without a model
to the core, which picks the model of the task, uses the key of the
client's agency and enforces the client's monthly quota for this app."""
import json

import requests
from django.conf import settings


class LLMError(Exception):
    pass


class QuotaExhausted(LLMError):
    """The client's monthly AI quota for the CRM is over: work manually."""


def call(tenant, task, request, timeout=180):
    response = requests.post(
        f"{settings.CORE_API_URL.rstrip('/')}/api/v1/llm/mensagens",
        json={"tenant": str(tenant.uuid), "task": task, "request": request},
        headers={"Authorization": f"Bearer {settings.CORE_WEBHOOK_SECRET}"},
        timeout=timeout,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"error": response.text[:300]}
    if response.status_code == 429:
        raise QuotaExhausted(data.get("error") or "cota de IA esgotada")
    if response.status_code != 200:
        raise LLMError(f"IA indisponível (HTTP {response.status_code}): {data.get('error', '')}"[:500])
    if data.get("stop_reason") == "refusal":
        raise LLMError("o modelo recusou o pedido")
    return data


def text_of(answer):
    """The answer's text (thinking blocks, if any, come first)."""
    return "".join(b.get("text", "") for b in answer.get("content", []) if b.get("type") == "text")


def json_of(answer):
    try:
        return json.loads(text_of(answer))
    except ValueError:
        raise LLMError("a IA não devolveu um JSON válido")
