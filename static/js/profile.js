const alertContainer = document.getElementById('alert-container');
const form = document.getElementById('profile-form');
const testBtn = document.getElementById('test-telegram-btn');
const testSheetBtn = document.getElementById('test-sheet-btn');
const detectTabsBtn = document.getElementById('detect-tabs-btn');
const syncNowBtn = document.getElementById('sync-now-btn');
const tabsList = document.getElementById('tabs-list');

let _detectedTabs = [];  // [{name, gid, detected}]

async function loadProfile() {
  const resp = await apiFetch('/api/profile/');
  if (!resp) return;
  const data = await resp.json();
  document.getElementById('sheet_id').value = data.sheet_id ?? '';
  document.getElementById('telegram_chat_id').value = data.telegram_chat_id ?? '';
  document.getElementById('telegram_enabled').checked = data.telegram_enabled ?? false;
  const botEl = document.getElementById('bot-name');
  if (botEl && BOT_NAME) botEl.textContent = '@' + BOT_NAME;

  // Restore tab selection from saved data
  if (data.selected_tab_names && data.selected_tab_names.length > 0) {
    renderTabsFromSaved(data.selected_tab_names);
  }
}

function renderTabsFromSaved(selectedNames) {
  // Show saved selection as read-only list until user re-detects
  tabsList.innerHTML = '';
  tabsList.style.display = 'flex';
  const hint = document.createElement('p');
  hint.className = 'form-hint';
  hint.textContent = `${selectedNames.length} aba(s) selecionada(s): ${selectedNames.join(', ')}. Clique em "Detectar Abas" para alterar.`;
  tabsList.appendChild(hint);
}

function renderDetectedTabs(tabs, selected) {
  _detectedTabs = tabs;
  tabsList.innerHTML = '';
  tabsList.style.display = 'flex';

  if (!tabs.length) {
    tabsList.innerHTML = '<p class="form-hint">Nenhuma aba detectada.</p>';
    return;
  }

  const selectedSet = new Set(selected || []);
  const allSelected = selectedSet.size === 0;  // empty = all shown

  for (const tab of tabs) {
    const item = document.createElement('label');
    item.className = 'tab-item';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.name = 'tab_select';
    cb.value = tab.name;
    cb.checked = allSelected || selectedSet.has(tab.name);

    const nameSpan = document.createElement('span');
    nameSpan.className = 'tab-item-name';
    nameSpan.textContent = tab.name;

    item.appendChild(cb);
    item.appendChild(nameSpan);

    if (tab.gid) {
      const gidSpan = document.createElement('span');
      gidSpan.className = 'tab-item-gid';
      gidSpan.textContent = `gid: ${tab.gid}`;
      item.appendChild(gidSpan);
    }

    tabsList.appendChild(item);
  }
}

function getSelectedTabNames() {
  const checkboxes = tabsList.querySelectorAll('input[type="checkbox"][name="tab_select"]');
  if (!checkboxes.length) return null;  // not yet detected — keep existing value
  const selected = [];
  checkboxes.forEach(cb => { if (cb.checked) selected.push(cb.value); });
  // If all checked → send [] (means "all")
  return selected.length === checkboxes.length ? [] : selected;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearAlert(alertContainer);

  const selectedTabs = getSelectedTabNames();
  const payload = {
    sheet_id: document.getElementById('sheet_id').value.trim(),
    telegram_chat_id: document.getElementById('telegram_chat_id').value.trim(),
    telegram_enabled: document.getElementById('telegram_enabled').checked,
  };
  if (selectedTabs !== null) {
    payload.selected_tab_names = selectedTabs;
  }

  const resp = await apiFetch('/api/profile/', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
  if (!resp) return;

  if (resp.ok) {
    showAlert(alertContainer, 'Perfil salvo com sucesso!', 'success');
  } else {
    const err = await resp.json();
    const msg = Object.values(err).flat().join(' ');
    showAlert(alertContainer, msg || 'Erro ao salvar.');
  }
});

detectTabsBtn.addEventListener('click', async () => {
  clearAlert(alertContainer);
  detectTabsBtn.disabled = true;
  detectTabsBtn.textContent = 'Detectando...';

  const resp = await apiFetch('/api/profile/detect-tabs/');
  detectTabsBtn.disabled = false;
  detectTabsBtn.textContent = 'Detectar Abas';
  if (!resp) return;

  const data = await resp.json();
  if (!resp.ok) {
    showAlert(alertContainer, data.error || 'Erro ao detectar abas.');
    return;
  }

  renderDetectedTabs(data.tabs, data.selected);
  showAlert(alertContainer,
    `${data.tabs.length} aba(s) encontrada(s). Selecione quais deseja exibir e clique em Salvar.`,
    'success');
});

testSheetBtn.addEventListener('click', async () => {
  clearAlert(alertContainer);
  testSheetBtn.disabled = true;
  testSheetBtn.textContent = 'Testando...';

  const resp = await apiFetch('/api/profile/test-sheet/', { method: 'POST' });
  testSheetBtn.disabled = false;
  testSheetBtn.textContent = 'Testar Planilha';
  if (!resp) return;

  const data = await resp.json();
  if (!resp.ok) {
    showAlert(alertContainer, data.error || 'Erro ao testar planilha.');
    return;
  }

  const lines = data.tabs.map(t =>
    t.ok
      ? `✓ "${t.name}": ${t.count} lead(s)`
      : `✗ "${t.name}": ${t.error}`
  );
  showAlert(alertContainer, lines.join('\n'), data.tabs.every(t => t.ok) ? 'success' : 'error');
});

testBtn.addEventListener('click', async () => {
  clearAlert(alertContainer);
  const resp = await apiFetch('/api/profile/test-telegram/', { method: 'POST' });
  if (!resp) return;

  if (resp.ok) {
    showAlert(alertContainer, 'Notificação de teste enviada! Verifique o Telegram.', 'success');
  } else {
    const err = await resp.json();
    showAlert(alertContainer, err.error || 'Erro ao enviar.');
  }
});

syncNowBtn.addEventListener('click', async () => {
  clearAlert(alertContainer);
  syncNowBtn.disabled = true;
  syncNowBtn.textContent = 'Verificando...';

  const resp = await apiFetch('/api/profile/sync-now/', { method: 'POST' });
  syncNowBtn.disabled = false;
  syncNowBtn.textContent = 'Verificar Leads Agora';
  if (!resp) return;

  const data = await resp.json();
  if (!resp.ok) {
    showAlert(alertContainer, data.error || 'Erro ao verificar leads.');
    return;
  }

  const lines = data.results.flatMap(r => {
    if (r.status === 'error') return [`✗ "${r.tab}": ${r.error}`];
    if (r.status === 'baseline') return [`📌 "${r.tab}": baseline definido (${r.count} leads)`];
    const notif = r.notifications_sent > 0
      ? `📢 ${r.notifications_sent} notificação(ões) enviada(s)`
      : (r.new_leads > 0 ? `⚠ ${r.new_leads} lead(s) novo(s) mas notificação desativada` : 'sem novos leads');
    const main = `"${r.tab}": ${r.previous}→${r.current} leads | ${notif}`;
    const cols = r.columns && r.columns.length ? [`   colunas: ${r.columns.join(', ')}`] : [];
    return [main, ...cols];
  });

  const type = data.results.some(r => r.status === 'error') ? 'error' : 'success';
  showAlert(alertContainer, lines.join('\n'), type);
});

loadProfile();
