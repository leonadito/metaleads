import logging

from django.contrib.auth.models import User
from django.utils import timezone

from .models import UserProfile, SheetMetadata, SyncLog
from .services.sheets import get_sheet_tabs, get_leads_gviz
from .services.telegram import send_new_lead_notification

logger = logging.getLogger(__name__)


def _extract_field(lead: dict, *keywords) -> str:
    """Return the value of the first key that contains any of the keywords (case-insensitive)."""
    for keyword in keywords:
        for key, value in lead.items():
            if keyword in key.lower() and value:
                return value
    return ''


def sync_user_now(profile: UserProfile) -> list[dict]:
    """Run one sync cycle for a single user and return per-tab diagnostics."""
    results = []
    user = profile.user
    sheet_id = profile.sheet_id

    from django.utils import timezone
    from datetime import timedelta

    meta = SheetMetadata.objects.filter(user=user, sheet_id=sheet_id).first()
    stale = (
        meta is None
        or not meta.sheet_names
        or meta.last_sync is None
        or (timezone.now() - meta.last_sync) > timedelta(hours=1)
    )
    if stale:
        tabs = get_sheet_tabs(sheet_id)
        meta, _ = SheetMetadata.objects.get_or_create(user=user, sheet_id=sheet_id)
        meta.sheet_names = tabs
        meta.last_sync = timezone.now()
        meta.save()
    else:
        tabs = meta.sheet_names

    for tab in tabs:
        try:
            result = _sync_tab(profile, tab)
        except Exception as exc:
            logger.exception("Erro ao sincronizar aba '%s'", tab['name'])
            result = {'tab': tab['name'], 'status': 'error', 'error': str(exc)}
        results.append(result)

    return results


def check_new_leads():
    """
    Runs every 5 minutes via APScheduler.
    For each active user with a configured sheet:
      1. Fetch CSV for each tab
      2. Compare row count against SyncLog
      3. Send Telegram notification for new leads
      4. Update SyncLog
    """
    profiles = UserProfile.objects.select_related('user').filter(
        user__is_active=True,
        sheet_id__gt='',
    )

    for profile in profiles:
        try:
            _sync_user(profile)
        except Exception:
            logger.exception("Erro ao sincronizar leads para %s", profile.user.email)


def _sync_user(profile: UserProfile):
    user = profile.user
    sheet_id = profile.sheet_id

    # Get or refresh tab list
    meta = SheetMetadata.objects.filter(user=user, sheet_id=sheet_id).first()
    if meta and meta.sheet_names:
        tabs = meta.sheet_names
    else:
        tabs = get_sheet_tabs(sheet_id)
        meta, _ = SheetMetadata.objects.get_or_create(user=user, sheet_id=sheet_id)
        meta.sheet_names = tabs
        meta.last_sync = timezone.now()
        meta.save()

    for tab in tabs:
        try:
            _sync_tab(profile, tab)
        except Exception:
            logger.exception(
                "Erro ao sincronizar aba '%s' para %s", tab['name'], user.email
            )


def _sync_tab(profile: UserProfile, tab: dict) -> dict:
    """Returns a diagnostic dict describing what happened for this tab."""
    user = profile.user
    sheet_name = tab['name']

    leads = get_leads_gviz(profile.sheet_id, tab['name'])
    current_count = len(leads)

    log, created = SyncLog.objects.get_or_create(
        user=user,
        sheet_name=sheet_name,
        defaults={'lead_count': current_count, 'last_lead_row_index': current_count},
    )

    if created:
        logger.info("[sync] %s | aba='%s' | baseline=%d", user.email, sheet_name, current_count)
        return {'tab': sheet_name, 'status': 'baseline', 'count': current_count}

    new_leads = leads[log.last_lead_row_index:]
    logger.info(
        "[sync] %s | aba='%s' | atual=%d | anterior=%d | novos=%d",
        user.email, sheet_name, current_count, log.last_lead_row_index, len(new_leads),
    )

    notifications_sent = 0
    for lead in new_leads:
        if profile.telegram_enabled and profile.telegram_chat_id:
            try:
                send_new_lead_notification(
                    chat_id=profile.telegram_chat_id,
                    sheet_name=sheet_name,
                    lead_name=_extract_field(lead, 'nome', 'name', 'cliente', 'lead'),
                    lead_phone=_extract_field(lead, 'telefone', 'fone', 'celular', 'whatsapp', 'phone', 'tel', 'contato'),
                )
                notifications_sent += 1
            except Exception:
                logger.exception("Falha ao enviar notificação Telegram")
        else:
            logger.warning(
                "[sync] Notificação pulada — telegram_enabled=%s chat_id='%s'",
                profile.telegram_enabled, profile.telegram_chat_id,
            )

    log.lead_count = current_count
    log.last_lead_row_index = current_count
    log.save()

    return {
        'tab': sheet_name,
        'status': 'ok',
        'previous': log.last_lead_row_index,
        'current': current_count,
        'new_leads': len(new_leads),
        'notifications_sent': notifications_sent,
        'telegram_enabled': profile.telegram_enabled,
        'has_chat_id': bool(profile.telegram_chat_id),
        'columns': list(leads[0].keys()) if leads else [],
    }
