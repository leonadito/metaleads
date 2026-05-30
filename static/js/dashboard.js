const loadingEl = document.getElementById('loading');
const errorEl = document.getElementById('error-container');
const contentEl = document.getElementById('dashboard-content');
const totalEl = document.getElementById('total-leads');
const sheetsGrid = document.getElementById('sheets-grid');
const refreshBtn = document.getElementById('refresh-btn');
const lastSyncEl = document.getElementById('last-sync');
const chartSection = document.getElementById('chart-section');

let chartInstance = null;

async function loadDashboard(forceRefresh = false) {
  loadingEl.style.display = 'block';
  contentEl.style.display = 'none';
  errorEl.style.display = 'none';

  const url = forceRefresh ? '/api/dashboard/?refresh=1' : '/api/dashboard/';
  const resp = await apiFetch(url);
  if (!resp) return;

  loadingEl.style.display = 'none';

  if (!resp.ok) {
    const err = await resp.json();
    errorEl.textContent = err.error || 'Erro ao carregar dashboard.';
    errorEl.style.display = 'block';
    return;
  }

  const data = await resp.json();
  contentEl.style.display = 'block';

  totalEl.textContent = data.total !== null && data.total !== undefined ? data.total : '—';
  renderSheets(data.sheets);

  if (lastSyncEl) {
    lastSyncEl.textContent = data.synced_at
      ? `Atualizado às ${data.synced_at}`
      : 'Nunca sincronizado — clique em ↻ Atualizar';
  }

  if (data.chart) {
    try { localStorage.setItem('dashboard_chart', JSON.stringify(data.chart)); } catch(_) {}
    chartSection.style.display = 'block';
    renderChart(data.chart);
  } else {
    const cached = (() => { try { return JSON.parse(localStorage.getItem('dashboard_chart')); } catch(_) { return null; } })();
    if (cached) {
      chartSection.style.display = 'block';
      renderChart(cached);
    } else {
      chartSection.style.display = 'none';
      if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
    }
  }
}

function renderSheets(sheets) {
  sheetsGrid.innerHTML = '';
  for (const sheet of sheets) {
    const card = document.createElement('div');
    card.className = 'sheet-card';
    const countDisplay = sheet.count !== null && sheet.count !== undefined ? sheet.count : '—';
    const errorBadge = sheet.error
      ? `<div class="sheet-card-error" title="${escHtml(sheet.error)}">⚠ erro ao carregar</div>`
      : '';
    card.innerHTML = `
      <div class="sheet-card-name">${escHtml(sheet.name)}</div>
      <div class="sheet-card-count">${countDisplay}</div>
      <div class="sheet-card-label">leads</div>
      ${errorBadge}
    `;
    if (!sheet.error) {
      card.addEventListener('click', () => {
        window.location.href = `/kanban/${encodeURIComponent(sheet.name)}/`;
      });
    }
    sheetsGrid.appendChild(card);
  }
}

function renderChart(chartData) {
  const labels = chartData.map(d => {
    const dt = new Date(d.date + 'T00:00:00');
    return dt.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
  });
  const counts = chartData.map(d => d.count);

  if (chartInstance) chartInstance.destroy();

  const ctx = document.getElementById('leads-chart').getContext('2d');
  chartInstance = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Leads',
        data: counts,
        backgroundColor: 'rgba(22,163,74,.6)',
        borderColor: 'rgba(22,163,74,1)',
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, ticks: { stepSize: 1 } },
        x: { ticks: { maxRotation: 45, font: { size: 10 } } },
      },
    },
  });
}

function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

refreshBtn.addEventListener('click', () => loadDashboard(true));
loadDashboard();
