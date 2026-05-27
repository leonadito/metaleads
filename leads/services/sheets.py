import json
import logging
import re
from urllib.parse import quote

import requests
import truststore

truststore.inject_into_ssl()

logger = logging.getLogger(__name__)

SHEETS_GVIZ_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:json&headers=1"
SHEETS_VALUES_URL = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range}"


def get_leads_gviz(sheet_id: str, tab_name: str | None = None) -> list[dict]:
    """
    Fetch sheet rows. Uses Sheets API v4 when GOOGLE_API_KEY is set (handles
    non-public sheets); falls back to the gviz public endpoint otherwise.
    """
    from django.conf import settings
    api_key = getattr(settings, 'GOOGLE_API_KEY', '')
    if api_key:
        return _get_leads_sheets_api(sheet_id, tab_name, api_key)
    return _get_leads_gviz_public(sheet_id, tab_name)


def _get_leads_sheets_api(sheet_id: str, tab_name: str | None, api_key: str) -> list[dict]:
    """Read rows via the Sheets API v4 (requires API key; sheet must allow at least link access)."""
    range_notation = f"'{tab_name}'!A:ZZ" if tab_name else "A:ZZ"
    url = SHEETS_VALUES_URL.format(sheet_id=sheet_id, range=quote(range_notation, safe=''))
    resp = requests.get(url, params={'key': api_key}, timeout=15)

    if resp.status_code == 403:
        raise ValueError(
            "Acesso negado. Verifique se a planilha está compartilhada como "
            "'Qualquer pessoa com o link pode visualizar' e se a API key tem permissão."
        )
    if resp.status_code == 404:
        raise ValueError("Planilha ou aba não encontrada. Verifique o ID e o nome da aba.")
    resp.raise_for_status()

    rows = resp.json().get('values', [])
    if not rows:
        return []

    headers = [h.strip().lower() for h in rows[0]]
    leads = []
    for row in rows[1:]:
        row_dict = {headers[i]: (row[i].strip() if i < len(row) else '') for i in range(len(headers))}
        if any(row_dict.values()):
            leads.append(row_dict)
    return leads


def _get_leads_gviz_public(sheet_id: str, tab_name: str | None) -> list[dict]:
    """Read rows via the public gviz endpoint (no API key; sheet must be fully public)."""
    url = SHEETS_GVIZ_URL.format(sheet_id=sheet_id)
    if tab_name:
        url += f"&sheet={quote(tab_name)}"

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    text = resp.text

    if text.lstrip().startswith('<!DOCTYPE') or text.lstrip().startswith('<html'):
        raise ValueError(
            "A planilha não está pública ou o ID está incorreto. "
            "Compartilhe como 'Qualquer pessoa com o link pode visualizar'."
        )

    match = re.search(r'setResponse\((.+)\);?\s*$', text, re.DOTALL)
    if not match:
        raise ValueError("Resposta inesperada do Google Sheets.")

    data = json.loads(match.group(1))

    if data.get('status') != 'ok':
        errors = data.get('errors') or []
        msg = '; '.join(e.get('detailed_message') or e.get('message', '') for e in errors)
        raise ValueError(f"Erro ao acessar planilha: {msg or 'status não ok'}")

    table = data.get('table') or {}
    cols = [c.get('label') or c.get('id') or '' for c in table.get('cols') or []]
    leads = []
    for row in table.get('rows') or []:
        cells = row.get('c') or []
        row_dict = {}
        for i, col_name in enumerate(cols):
            cell = cells[i] if i < len(cells) else None
            val = (cell.get('v') if cell else None)
            if val is None and cell:
                val = cell.get('f')
            row_dict[col_name.strip().lower()] = '' if val is None else str(val).strip()
        if any(row_dict.values()):
            leads.append(row_dict)
    return leads


def get_sheet_tabs(sheet_id: str) -> list[dict]:
    """
    Returns all tabs for a public Google Sheet via the Sheets API v4.
    Requires GOOGLE_API_KEY in .env. Each entry: {'name', 'gid', 'detected'}.
    """
    from django.conf import settings
    api_key = getattr(settings, 'GOOGLE_API_KEY', '')
    if not api_key:
        raise ValueError(
            "GOOGLE_API_KEY não configurada. Adicione a chave no arquivo .env."
        )

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
    resp = requests.get(
        url,
        params={'key': api_key, 'fields': 'sheets.properties(sheetId,title)'},
        timeout=10,
    )

    if resp.status_code == 403:
        raise ValueError(
            "Acesso negado pela API do Google. Verifique se a chave é válida e se "
            "a planilha está compartilhada como 'Qualquer pessoa com o link pode visualizar'."
        )
    if resp.status_code == 404:
        raise ValueError("Planilha não encontrada. Verifique o ID da planilha.")
    resp.raise_for_status()

    tabs = []
    for sheet in resp.json().get('sheets', []):
        props = sheet.get('properties', {})
        name = props.get('title', '').strip()
        gid = str(props.get('sheetId', ''))
        if name:
            tabs.append({'name': name, 'gid': gid, 'detected': True})

    logger.info("Abas via Sheets API v4 para %s: %s", sheet_id, [t['name'] for t in tabs])
    return tabs


def update_lead_status(sheet_id: str, tab_name: str, row_index: int, new_status: str) -> None:
    """Write new_status to the lead_status column of the given row via the Sheets API v4."""
    from django.conf import settings
    from pathlib import Path

    sa_path = getattr(settings, 'GOOGLE_SERVICE_ACCOUNT_JSON', '')
    if not sa_path:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON não configurado no .env")

    sa_file = Path(settings.BASE_DIR) / sa_path
    if not sa_file.exists():
        raise ValueError(f"Arquivo de conta de serviço não encontrado: {sa_file}")

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError("Execute: pip install google-auth google-api-python-client")

    creds = service_account.Credentials.from_service_account_file(
        str(sa_file),
        scopes=['https://www.googleapis.com/auth/spreadsheets'],
    )
    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    sheets_svc = service.spreadsheets()

    # Find the status column index by reading the header row
    header_result = sheets_svc.values().get(
        spreadsheetId=sheet_id,
        range=f"'{tab_name}'!1:1",
    ).execute()
    headers = [h.strip().lower() for h in (header_result.get('values') or [[]])[0]]

    col_idx = None
    for candidate in ['lead_status', 'status']:
        if candidate in headers:
            col_idx = headers.index(candidate)
            break
    if col_idx is None:
        for i, h in enumerate(headers):
            if 'status' in h:
                col_idx = i
                break
    if col_idx is None:
        raise ValueError(f"Coluna de status não encontrada na aba '{tab_name}'. Colunas: {headers}")

    cell_range = f"'{tab_name}'!{_col_index_to_letter(col_idx)}{row_index}"
    sheets_svc.values().update(
        spreadsheetId=sheet_id,
        range=cell_range,
        valueInputOption='USER_ENTERED',
        body={'values': [[new_status]]},
    ).execute()

    logger.info("Lead status atualizado: %s linha %s → %s", tab_name, row_index, new_status)


def _col_index_to_letter(idx: int) -> str:
    """Convert 0-based column index to A1 column letter (0→A, 25→Z, 26→AA)."""
    letter = ''
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letter = chr(65 + rem) + letter
    return letter
