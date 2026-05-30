import csv
import io
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import UserProfile, SheetMetadata, SyncLog
from .serializers import UserProfileSerializer
from .services.sheets import get_sheet_tabs, get_leads_gviz
from .services.telegram import send_test_notification

KANBAN_COLUMNS = [
    ('criado', 'Criado'),
    ('em_analise', 'Em Análise'),
    ('qualificado', 'Qualificado'),
    ('nao_qualificado', 'Não Qualificado'),
    ('convertido', 'Convertido'),
    ('perdido', 'Perdido'),
]

STATUS_MAP = {
    'criado': 'CRIADO',
    'em_analise': 'EM_ANALISE',
    'qualificado': 'QUALIFICADO',
    'nao_qualificado': 'NAO_QUALIFICADO',
    'convertido': 'CONVERTIDO',
    'perdido': 'PERDIDO',
}

# Valores exatos gravados na coluna lead_status conforme documentação Meta Ads
SHEET_WRITE_VALUES = {
    'criado': 'Criado',
    'em_analise': 'Em análise',
    'qualificado': 'Qualificado',
    'nao_qualificado': 'Não qualificado',
    'convertido': 'Convertido',
    'perdido': 'Perdido',
}


def _get_or_create_profile(user):
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


# ── Page views ──────────────────────────────────────────────────────────────

@login_required
def dashboard_view(request):
    profile = _get_or_create_profile(request.user)
    if not profile.is_configured:
        return redirect('profile')
    return render(request, 'dashboard.html')


@login_required
def profile_view(request):
    from django.conf import settings
    return render(request, 'profile.html', {'bot_name': settings.TELEGRAM_BOT_NAME})


@login_required
def kanban_view(request, sheet_name):
    return render(request, 'kanban.html', {'sheet_name': sheet_name})


# ── API endpoints ────────────────────────────────────────────────────────────

@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def profile_api(request):
    profile = _get_or_create_profile(request.user)

    if request.method == 'GET':
        serializer = UserProfileSerializer(profile)
        return Response(serializer.data)

    serializer = UserProfileSerializer(profile, data=request.data, partial=True)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    serializer.save()

    # Refresh sheet tabs when sheet_id changes
    new_sheet_id = serializer.validated_data.get('sheet_id', profile.sheet_id)
    if new_sheet_id:
        try:
            tabs = get_sheet_tabs(new_sheet_id)
            meta, _ = SheetMetadata.objects.get_or_create(user=request.user, sheet_id=new_sheet_id)
            meta.sheet_names = tabs
            meta.last_sync = timezone.now()
            meta.save()
        except Exception:
            pass  # Tabs will be fetched lazily on dashboard load

    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def sync_now(request):
    """Manually trigger one lead-sync cycle and return per-tab diagnostics."""
    profile = _get_or_create_profile(request.user)
    if not profile.is_configured:
        return Response({'error': 'Planilha não configurada.'}, status=400)
    from .tasks import sync_user_now
    try:
        results = sync_user_now(profile)
    except Exception as exc:
        return Response({'error': str(exc)}, status=502)
    return Response({'results': results})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_telegram(request):
    profile = _get_or_create_profile(request.user)
    if not profile.telegram_chat_id:
        return Response({'error': 'Chat ID do Telegram não configurado.'}, status=400)
    try:
        send_test_notification(profile.telegram_chat_id)
        return Response({'ok': True})
    except Exception as exc:
        return Response({'error': str(exc)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def detect_tabs(request):
    """Detect available sheet tabs and return them for the profile UI."""
    profile = _get_or_create_profile(request.user)
    if not profile.sheet_id:
        return Response({'error': 'Nenhuma planilha configurada.'}, status=400)

    SheetMetadata.objects.filter(user=request.user, sheet_id=profile.sheet_id).delete()
    try:
        tabs = get_sheet_tabs(profile.sheet_id)
    except Exception as exc:
        return Response({'error': str(exc)}, status=502)

    meta, _ = SheetMetadata.objects.get_or_create(user=request.user, sheet_id=profile.sheet_id)
    meta.sheet_names = tabs
    from django.utils import timezone
    meta.last_sync = timezone.now()
    meta.save()

    return Response({'tabs': tabs, 'selected': profile.selected_tab_names})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_sheet(request):
    profile = _get_or_create_profile(request.user)
    if not profile.sheet_id:
        return Response({'error': 'Nenhuma planilha configurada.'}, status=400)

    SheetMetadata.objects.filter(user=request.user, sheet_id=profile.sheet_id).delete()

    try:
        tabs = get_sheet_tabs(profile.sheet_id)
    except Exception as exc:
        return Response({'error': f'Erro ao detectar abas: {exc}'}, status=502)

    results = []
    for tab in tabs:
        try:
            leads = get_leads_gviz(profile.sheet_id, _gviz_tab_name(tab))
            results.append({'name': tab['name'], 'gid': tab['gid'], 'count': len(leads), 'ok': True})
        except Exception as exc:
            results.append({'name': tab['name'], 'gid': tab['gid'], 'count': 0, 'ok': False, 'error': str(exc)})

    return Response({'tabs': results})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_api(request):
    profile = _get_or_create_profile(request.user)
    if not profile.is_configured:
        return Response({'error': 'Planilha não configurada.'}, status=400)

    force_refresh = request.query_params.get('refresh') == '1'

    if force_refresh:
        SheetMetadata.objects.filter(user=request.user, sheet_id=profile.sheet_id).delete()

    try:
        tabs = _get_tabs(request.user, profile.sheet_id)
    except Exception as exc:
        return Response({'error': f'Erro ao buscar planilha: {exc}'}, status=502)

    selected = profile.selected_tab_names
    active_tabs = [t for t in tabs if not selected or t['name'] in selected]

    if not force_refresh:
        return _dashboard_from_cache(request.user, profile.sheet_id, active_tabs)

    return _dashboard_from_api(request.user, profile.sheet_id, active_tabs)


def _dashboard_from_cache(user, profile_sheet_id, active_tabs):
    """Serve counts from SyncLog and chart from SheetMetadata — zero Google API calls."""
    logs = {log.sheet_name: log for log in SyncLog.objects.filter(user=user)}
    sheets_data = []
    total = 0
    latest_sync = None

    for tab in active_tabs:
        log = logs.get(tab['name'])
        count = log.lead_count if log else None
        if count is not None:
            total += count
        if log and (latest_sync is None or log.synced_at > latest_sync):
            latest_sync = log.synced_at
        sheets_data.append({'name': tab['name'], 'gid': tab['gid'], 'count': count})

    synced_at_str = None
    if latest_sync:
        local_dt = timezone.localtime(latest_sync)
        synced_at_str = local_dt.strftime('%d/%m/%Y %H:%M')

    meta = SheetMetadata.objects.filter(user=user, sheet_id=profile_sheet_id).first()
    chart = meta.chart_data if meta else None

    return Response({
        'total': total if logs else None,
        'sheets': sheets_data,
        'chart': chart,
        'synced_at': synced_at_str,
    })


def _dashboard_from_api(user, sheet_id, active_tabs):
    """Fetch fresh data from Google API, update SyncLog, return with chart."""
    import time as _time

    sheets_data = []
    all_leads = []
    total = 0

    for idx, tab in enumerate(active_tabs):
        if idx > 0:
            _time.sleep(1.0)
        try:
            leads = get_leads_gviz(sheet_id, _gviz_tab_name(tab))
            count = len(leads)
            total += count
            all_leads.extend(leads)
            sheets_data.append({'name': tab['name'], 'gid': tab['gid'], 'count': count})
            log, _ = SyncLog.objects.get_or_create(
                user=user,
                sheet_name=tab['name'],
                defaults={'lead_count': count, 'last_lead_row_index': count},
            )
            log.lead_count = count
            log.last_lead_row_index = count
            log.save()
        except Exception as exc:
            sheets_data.append({'name': tab['name'], 'gid': tab['gid'], 'count': None, 'error': str(exc)})

    chart = _build_chart_data(all_leads)
    SheetMetadata.objects.filter(user=user, sheet_id=sheet_id).update(chart_data=chart)

    local_dt = timezone.localtime(timezone.now())
    return Response({
        'total': total,
        'sheets': sheets_data,
        'chart': chart,
        'synced_at': local_dt.strftime('%d/%m/%Y %H:%M'),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sheets_api(request):
    profile = _get_or_create_profile(request.user)
    if not profile.is_configured:
        return Response({'error': 'Planilha não configurada.'}, status=400)
    tabs = _get_tabs(request.user, profile.sheet_id)
    return Response({'sheets': tabs})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def kanban_api(request, sheet_name):
    profile = _get_or_create_profile(request.user)
    if not profile.is_configured:
        return Response({'error': 'Planilha não configurada.'}, status=400)

    tabs = _get_tabs(request.user, profile.sheet_id)
    tab = next((t for t in tabs if t['name'] == sheet_name), None)
    if not tab:
        return Response({'error': 'Aba não encontrada.'}, status=404)

    try:
        leads = get_leads_gviz(profile.sheet_id, _gviz_tab_name(tab))
    except Exception as exc:
        return Response({'error': str(exc)}, status=502)

    from datetime import date as _date_cls
    leads_indexed = list(enumerate(leads))
    leads_indexed.sort(
        key=lambda t: _parse_lead_date(t[1]) or _date_cls.min,
        reverse=True,
    )

    columns = {col_id: [] for col_id, _ in KANBAN_COLUMNS}
    for i, lead in leads_indexed:
        raw_status = _normalize_status(_pick_field(lead, ['status', 'lead_status']) or '')
        col_id = _status_to_column(raw_status)
        columns[col_id].append({
            'row_index': i + 2,  # 1-based, row 1 is header
            'name': _pick_field(lead, ['nome_completo', 'nome', 'name', 'full_name']),
            'phone': _pick_field(lead, ['número_do_whatsapp', 'whatsapp', 'telefone', 'phone', 'celular', 'tel']),
            'date': _format_date_display(_pick_field(lead, ['created_time', 'data', 'date', 'carimbo', 'timestamp'])),
            'status': raw_status,
            'fields': {k: v for k, v in lead.items() if v},
        })

    result = [
        {'id': col_id, 'label': label, 'leads': columns[col_id]}
        for col_id, label in KANBAN_COLUMNS
    ]
    return Response({'sheet_name': sheet_name, 'columns': result})


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def lead_update(request, row_index):
    """Update lead status in Google Sheets."""
    profile = _get_or_create_profile(request.user)
    if not profile.is_configured:
        return Response({'error': 'Planilha não configurada.'}, status=400)

    new_column_id = request.data.get('new_column_id')
    if new_column_id not in STATUS_MAP:
        return Response({'error': 'Status inválido.'}, status=400)

    sheet_status = SHEET_WRITE_VALUES[new_column_id]

    sheet_name = request.data.get('sheet_name', '')
    tabs = _get_tabs(request.user, profile.sheet_id)
    tab = next((t for t in tabs if t['name'] == sheet_name), None)
    if not tab:
        return Response({'error': 'Aba não encontrada.'}, status=404)

    from .services.sheets import update_lead_status
    try:
        update_lead_status(profile.sheet_id, tab['name'], row_index, sheet_status)
    except Exception as exc:
        return Response({'error': str(exc)}, status=502)

    return Response({'ok': True, 'new_status': STATUS_MAP[new_column_id]})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize_status(raw: str) -> str:
    """Strip accents, uppercase, replace spaces with underscores — for status column lookup."""
    import unicodedata
    normalized = unicodedata.normalize('NFKD', raw).encode('ascii', 'ignore').decode('ascii')
    return normalized.strip().upper().replace(' ', '_')


def _pick_field(lead: dict, candidates: list) -> str:
    """Return first non-empty value whose key matches any candidate (exact then substring)."""
    for key in candidates:
        if key in lead and lead[key]:
            return lead[key]
    for key in candidates:
        for k, v in lead.items():
            if key in k and v:
                return v
    return ''


def _gviz_tab_name(tab: dict) -> str | None:
    """Return real tab name for gviz if detected; None means first sheet (fallback)."""
    if tab.get('detected') or tab.get('gid') is not None:
        return tab['name']
    return None


def _get_tabs(user, sheet_id):
    """Return tabs from cache if fresh (< 1h), otherwise fetch and cache."""
    meta = SheetMetadata.objects.filter(user=user, sheet_id=sheet_id).first()
    stale = (
        meta is None
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
    return tabs


def _status_to_column(raw_status):
    mapping = {
        'CRIADO': 'criado',
        'EM_ANALISE': 'em_analise',
        'QUALIFICADO': 'qualificado',
        'NAO_QUALIFICADO': 'nao_qualificado',
        'CONVERTIDO': 'convertido',
        'PERDIDO': 'perdido',
    }
    return mapping.get(raw_status, 'criado')


def _format_date_display(raw: str) -> str:
    """Convert raw date/datetime string to DD/MM/YYYY HH:MM for card display."""
    import re
    if not raw:
        return ''
    raw = raw.strip()

    # Google Sheets gviz: Date(YYYY, M_0idx, D[, H, Min, S])
    m = re.match(r'Date\((\d+),(\d+),(\d+)(?:,(\d+),(\d+))?', raw, re.IGNORECASE)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)) + 1, int(m.group(3))
            if m.group(4) is not None:
                return f'{d:02d}/{mo:02d}/{y} {int(m.group(4)):02d}:{int(m.group(5)):02d}'
            return f'{d:02d}/{mo:02d}/{y}'
        except (ValueError, TypeError):
            pass

    # ISO: 2026-05-12T02:17:50[-05:00]
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})', raw)
    if m:
        return f'{m.group(3)}/{m.group(2)}/{m.group(1)} {m.group(4)}:{m.group(5)}'

    # ISO date only: 2026-05-12
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', raw)
    if m:
        return f'{m.group(3)}/{m.group(2)}/{m.group(1)}'

    # Already DD/MM/YYYY HH:MM[:SS]
    m = re.match(r'(\d{2}/\d{2}/\d{4}) (\d{2}:\d{2})', raw)
    if m:
        return f'{m.group(1)} {m.group(2)}'

    return raw


def _parse_lead_date(lead: dict):
    """Return a date extracted from the lead's date/timestamp column, or None."""
    import re as _re
    from datetime import date as _date, datetime as _dt
    import logging as _logging
    _log = _logging.getLogger(__name__)

    date_keywords = ['carimbo', 'timestamp', 'created_time', 'created', 'data', 'date', 'hora', 'criado', 'enviado', 'registrado']
    raw = None
    matched_key = None
    for keyword in date_keywords:
        for key, value in lead.items():
            if keyword in key.lower() and value:
                raw = value
                matched_key = key
                break
        if raw:
            break

    if not raw:
        return None

    raw = raw.strip()

    # Google Sheets gviz Date/DateTime: Date(YYYY,M_0idx,D) or Date(YYYY,M_0idx,D,H,Min,S)
    m = _re.match(r'Date\((\d+),(\d+),(\d+)', raw, _re.IGNORECASE)
    if m:
        try:
            return _date(int(m.group(1)), int(m.group(2)) + 1, int(m.group(3)))
        except ValueError:
            pass

    # Strip time: handles "2026-05-08T09:53:33-05:00" and "25/05/2025 09:30:00"
    date_part = raw.split('T')[0].split()[0]
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%m/%d/%Y', '%d-%m-%Y', '%d/%m/%y'):
        try:
            return _dt.strptime(date_part, fmt).date()
        except ValueError:
            continue

    return None


def _build_chart_data(all_leads: list) -> list:
    """Count leads per day over the last 30 days using the actual date column from each lead."""
    today = timezone.now().date()
    days = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
    counts = {d: 0 for d in days}

    for lead in all_leads:
        d = _parse_lead_date(lead)
        if d and d in counts:
            counts[d] += 1

    return [{'date': str(d), 'count': counts[d]} for d in days]
