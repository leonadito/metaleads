/* Read sheet name injected by Django template */
const sheetNameEl = document.getElementById('sheet-name-data');
const SHEET_NAME = sheetNameEl ? JSON.parse(sheetNameEl.textContent) : '';

const loadingEl = document.getElementById('loading');
const errorEl = document.getElementById('error-container');
const board = document.getElementById('kanban-board');
const refreshBtn = document.getElementById('refresh-btn');

let draggedCard = null;
let draggedLead = null;

async function loadKanban() {
  loadingEl.style.display = 'block';
  board.style.display = 'none';
  errorEl.style.display = 'none';

  const resp = await apiFetch(`/api/kanban/${encodeURIComponent(SHEET_NAME)}/`);
  if (!resp) return;

  loadingEl.style.display = 'none';

  if (!resp.ok) {
    const err = await resp.json();
    errorEl.textContent = err.error || 'Erro ao carregar Kanban.';
    errorEl.style.display = 'block';
    return;
  }

  const data = await resp.json();
  renderBoard(data.columns);
  board.style.display = 'flex';
}

function renderBoard(columns) {
  board.innerHTML = '';
  for (const col of columns) {
    board.appendChild(buildColumn(col));
  }
}

function buildColumn(col) {
  const colEl = document.createElement('div');
  colEl.className = 'kanban-column';
  colEl.dataset.colId = col.id;

  const header = document.createElement('div');
  header.className = 'kanban-column-header';
  header.innerHTML = `
    <span>${escHtml(col.label)}</span>
    <span class="kanban-column-count" id="count-${col.id}">${col.leads.length}</span>
  `;

  const body = document.createElement('div');
  body.className = 'kanban-column-body';
  body.dataset.colId = col.id;

  for (const lead of col.leads) {
    body.appendChild(buildCard(lead));
  }

  // Drag-over target
  body.addEventListener('dragover', (e) => {
    e.preventDefault();
    body.classList.add('drag-over');
  });

  body.addEventListener('dragleave', () => body.classList.remove('drag-over'));

  body.addEventListener('drop', async (e) => {
    e.preventDefault();
    body.classList.remove('drag-over');
    if (!draggedCard || !draggedLead) return;

    const newColId = body.dataset.colId;
    const oldColId = draggedCard.closest('.kanban-column-body')?.dataset.colId;
    if (!newColId || newColId === oldColId) return;

    // Optimistic UI update
    const oldCount = draggedCard.closest('.kanban-column-body');
    body.appendChild(draggedCard);
    updateCount(oldColId, -1);
    updateCount(newColId, +1);

    // Persist to backend
    const resp = await apiFetch(`/api/lead/${draggedLead.row_index}/`, {
      method: 'PATCH',
      body: JSON.stringify({ new_column_id: newColId, sheet_name: SHEET_NAME }),
    });

    if (!resp || !resp.ok) {
      // Rollback on error
      if (oldCount) oldCount.appendChild(draggedCard);
      updateCount(oldColId, +1);
      updateCount(newColId, -1);
      const err = resp ? await resp.json() : {};
      alert(err.error || 'Erro ao atualizar status.');
    }

    draggedCard = null;
    draggedLead = null;
  });

  colEl.appendChild(header);
  colEl.appendChild(body);
  return colEl;
}

function buildCard(lead) {
  const card = document.createElement('div');
  card.className = 'lead-card';
  card.draggable = true;
  card.innerHTML = `
    <div class="lead-card-name">${escHtml(lead.name || '—')}</div>
    <div class="lead-card-phone">${escHtml(lead.phone || '')}</div>
    <div class="lead-card-date">${escHtml(lead.date || '')}</div>
  `;

  card.addEventListener('dragstart', () => {
    draggedCard = card;
    draggedLead = lead;
    card.classList.add('dragging');
  });

  card.addEventListener('dragend', () => {
    card.classList.remove('dragging');
  });

  return card;
}

function updateCount(colId, delta) {
  const el = document.getElementById(`count-${colId}`);
  if (el) el.textContent = parseInt(el.textContent, 10) + delta;
}

function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

refreshBtn.addEventListener('click', loadKanban);
loadKanban();
