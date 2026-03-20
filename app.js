// Bitcraft Market Advisor — Frontend Logic

const API = ''; // relative URLs — works locally and on Vercel

let playerId = null;
let playerTools = {};
let allItems = [];
let sortCol = 'score';
let sortAsc = false;

// ── Player search ──────────────────────────────────────────────────────────

async function searchPlayer() {
  const username = document.getElementById('username-input').value.trim();
  if (!username) return;

  setLoading('Looking up player…');
  hideError();

  try {
    const res = await fetch(`${API}/api/search?username=${encodeURIComponent(username)}`);
    const data = await res.json();

    if (!res.ok || data.error) {
      showError(data.error || 'Player not found.');
      stopLoading();
      return;
    }

    playerId = data.player_id;
    playerTools = data.tools;

    renderTools(data.tools);
    document.getElementById('tools-display').style.display = 'block';
    document.getElementById('filters-card').style.display = 'block';

    stopLoading();
    await loadRegions();
    await fetchResults();

  } catch (e) {
    showError('Failed to reach the server. Please try again.');
    stopLoading();
  }
}

function renderTools(tools) {
  const grid = document.getElementById('tools-grid');
  grid.innerHTML = '';
  const sorted = Object.values(tools).sort((a, b) => b.level - a.level);
  for (const t of sorted) {
    const chip = document.createElement('div');
    chip.className = 'tool-chip';
    chip.textContent = `${t.name} (L${t.level})`;
    grid.appendChild(chip);
  }
}

// ── Regions ────────────────────────────────────────────────────────────────

async function loadRegions() {
  const grid = document.getElementById('regions-grid');
  grid.innerHTML = '<span style="color:var(--fg-dim);font-size:12px">Loading regions…</span>';

  try {
    const res  = await fetch(`${API}/api/regions`);
    const data = await res.json();
    const regions = data.regions || [];

    grid.innerHTML = '';
    for (const r of regions) {
      const chip = document.createElement('label');
      chip.className = 'region-chip';
      chip.dataset.id = r.id;
      chip.innerHTML = `
        <input type="checkbox" value="${r.id}" onchange="toggleRegion(this)" />
        <div class="dot"></div>
        <span>${r.name || 'Region ' + r.id}</span>
        <span class="region-players">(${r.players ?? '?'})</span>
      `;
      grid.appendChild(chip);
    }
  } catch (e) {
    grid.innerHTML = '<span style="color:var(--fg-dim);font-size:12px">Could not load regions.</span>';
  }
}

function toggleRegion(checkbox) {
  const chip = checkbox.closest('.region-chip');
  chip.classList.toggle('active', checkbox.checked);
}

function getSelectedRegions() {
  return [...document.querySelectorAll('#regions-grid input:checked')]
    .map(cb => cb.value)
    .join(',');
}

// ── Results ────────────────────────────────────────────────────────────────

async function fetchResults() {
  if (!playerId) return;

  const minPrice  = document.getElementById('min-price').value || 1;
  const crafting  = document.getElementById('crafting-toggle').checked;
  const debug     = document.getElementById('debug-toggle').checked;
  const regions   = getSelectedRegions();

  const params = new URLSearchParams({
    player_id: playerId,
    min_price: minPrice,
    crafting:  crafting,
    debug:     debug,
  });
  if (regions) params.set('regions', regions);

  setLoading('Fetching market data…');
  hideError();
  document.getElementById('results-section').style.display = 'none';
  document.getElementById('refresh-btn').disabled = true;

  try {
    const res  = await fetch(`${API}/api/results?${params}`);
    const data = await res.json();

    if (!res.ok || data.error) {
      showError(data.error || 'Failed to fetch results.');
      stopLoading();
      document.getElementById('refresh-btn').disabled = false;
      return;
    }

    allItems = data.items || [];
    renderStats(data.stats, regions);
    renderTable();

    document.getElementById('results-section').style.display = 'block';
    stopLoading();
    document.getElementById('refresh-btn').disabled = false;

  } catch (e) {
    showError('Failed to reach the server. Please try again.');
    stopLoading();
    document.getElementById('refresh-btn').disabled = false;
  }
}

function renderStats(stats, regions) {
  const regionLabel = regions
    ? regions.split(',').map(r => {
        const chip = document.querySelector(`.region-chip[data-id="${r}"] span:not(.region-players)`);
        return chip ? chip.textContent : `Region ${r}`;
      }).join(', ')
    : 'All Regions';

  document.getElementById('results-title').textContent =
    `Buy Opportunities — ${regionLabel}`;

  const pills = document.getElementById('stats-pills');
  pills.innerHTML = `
    <div class="pill">Market items <span>${stats.total_market ?? '?'}</span></div>
    <div class="pill">Recipes cached <span>${stats.cached_recipes ?? '?'}</span></div>
    <div class="pill">Gatherable <span>${stats.extractable ?? '?'}</span></div>
    <div class="pill">Craftable <span>${stats.craftable ?? '?'}</span></div>
    <div class="pill">With orders <span>${stats.with_orders ?? '?'}</span></div>
    ${stats.unobtainable ? `<div class="pill">Unobtainable <span>${stats.unobtainable}</span></div>` : ''}
  `;
}

// ── Table ──────────────────────────────────────────────────────────────────

function renderTable() {
  const sorted = [...allItems].sort((a, b) => {
    let av = a[sortCol], bv = b[sortCol];
    if (typeof av === 'string') av = av.toLowerCase(), bv = bv.toLowerCase();
    if (av < bv) return sortAsc ? -1 :  1;
    if (av > bv) return sortAsc ?  1 : -1;
    return 0;
  });

  const tbody = document.getElementById('results-body');
  tbody.innerHTML = '';

  if (sorted.length === 0) {
    document.getElementById('empty-msg').style.display = 'block';
    return;
  }
  document.getElementById('empty-msg').style.display = 'none';

  for (const r of sorted) {
    const tierTag    = r.tier >= 0 ? `T${r.tier}` : (r.tag || '—');
    const buyStr     = r.highest_buy != null ? r.highest_buy.toLocaleString() : '—';
    const qtyStr     = r.total_qty   != null ? r.total_qty.toLocaleString()   : '—';
    const scoreStr   = r.score       > 0     ? r.score.toLocaleString()       : '—';
    const tr = document.createElement('tr');
    if (r.source === 'none') tr.classList.add('unobtainable');
    tr.innerHTML = `
      <td>${escHtml(r.name)}</td>
      <td class="tier-tag">${escHtml(tierTag)}</td>
      <td><span class="badge ${r.source}">${r.source}</span></td>
      <td class="num">${buyStr}</td>
      <td class="num">${qtyStr}</td>
      <td class="num">${scoreStr}</td>
    `;
    tbody.appendChild(tr);
  }
}

function sortBy(col) {
  if (sortCol === col) {
    sortAsc = !sortAsc;
  } else {
    sortCol = col;
    sortAsc = ['name', 'tier', 'source'].includes(col);
  }

  // Update arrows
  document.querySelectorAll('.sort-arrow').forEach(el => el.textContent = '');
  const arrow = document.getElementById(`arrow-${col}`);
  if (arrow) arrow.textContent = sortAsc ? '▲' : '▼';

  document.querySelectorAll('th').forEach(th => th.classList.remove('sorted'));
  const ths = document.querySelectorAll('th');
  ths.forEach(th => { if (th.querySelector(`#arrow-${col}`)) th.classList.add('sorted'); });

  renderTable();
}

// ── UI helpers ─────────────────────────────────────────────────────────────

function setLoading(msg) {
  document.getElementById('loading-msg').textContent = msg;
  document.getElementById('loading').style.display = 'block';
}
function stopLoading() {
  document.getElementById('loading').style.display = 'none';
}
function showError(msg) {
  const el = document.getElementById('error-msg');
  document.getElementById('error-text').textContent = msg;
  el.style.display = 'block';
}
function hideError() {
  document.getElementById('error-msg').style.display = 'none';
}
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function toggleDebug() {
  if (playerId) fetchResults();
}

// Allow pressing Enter in the username field
document.getElementById('username-input')
  .addEventListener('keydown', e => { if (e.key === 'Enter') searchPlayer(); });
