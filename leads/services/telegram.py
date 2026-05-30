import logging

import platform

import requests
from django.conf import settings

if platform.system() == 'Windows':
    import truststore
    truststore.inject_into_ssl()

logger = logging.getLogger(__name__)

_last_update_id: int | None = None
_session: requests.Session | None = None

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _post(token: str, method: str, **kwargs) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    resp = _get_session().post(url, timeout=10, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _get(token: str, method: str, **kwargs) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    resp = _get_session().get(url, timeout=10, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _send_message(chat_id: str, text: str) -> None:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN não configurado.")
    data = _post(token, "sendMessage", json={"chat_id": chat_id, "text": text})
    if not data.get('ok'):
        raise RuntimeError(f"Telegram API error: {data.get('description')}")


def send_new_lead_notification(chat_id: str, sheet_name: str, lead_name: str, lead_phone: str) -> None:
    from urllib.parse import quote
    site_url = settings.SITE_URL.rstrip('/')
    kanban_url = f"{site_url}/kanban/{quote(sheet_name, safe='')}/"
    text = (
        "📢 Novo Lead!\n"
        f"📌 Aba: {sheet_name}\n"
        f"👤 Nome: {lead_name}\n"
        f"📱 Telefone: {lead_phone}\n"
        f"🔗 Acesse: {kanban_url}"
    )
    _send_message(chat_id, text)


def send_test_notification(chat_id: str) -> None:
    _send_message(chat_id, "✅ MetaLeads - Agitare\nNotificações Telegram configuradas com sucesso!")


def poll_and_reply_start() -> None:
    """
    Polls Telegram getUpdates and replies to /start with the sender's Chat ID.
    Called every 30s by APScheduler.
    """
    global _last_update_id

    token = settings.TELEGRAM_BOT_TOKEN
    if not token:
        return

    params = {"timeout": 0, "limit": 50}
    if _last_update_id is not None:
        params["offset"] = _last_update_id + 1

    try:
        data = _get(token, "getUpdates", params=params)
        updates = data.get("result", [])
    except Exception:
        logger.exception("Erro ao buscar updates do Telegram")
        return

    for update in updates:
        _last_update_id = update["update_id"]
        message = update.get("message", {})
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        first_name = chat.get("first_name", "")

        if text.startswith("/start") and chat_id:
            reply = (
                f"Olá{', ' + first_name if first_name else ''}! 👋\n\n"
                f"Seu Chat ID é:\n{chat_id}\n\n"
                "Cole esse número em MetaLeads - Agitare → Meu Perfil → Chat ID do Telegram "
                "e ative as notificações."
            )
            try:
                _post(token, "sendMessage", json={"chat_id": chat_id, "text": reply})
                logger.info("Chat ID enviado para %s (%s)", first_name, chat_id)
            except Exception:
                logger.exception("Erro ao responder /start para chat_id=%s", chat_id)
