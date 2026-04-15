// Bitcraft Market Advisor — Frontend Logic

const API = ''; // relative URLs — works locally and on Vercel

let playerId   = null;
let playerTools = {};
let allItems   = [];
let sortCol    = 'score';
let sortAsc    = false;
let activeTags = new Set();   // empty = show all tags

// ── Player search ──────────────────────────────────────────────────────────

async function searchPlayer() {
  const username = document.getElementById('username-input').value.trim();
  if (!username) return;
  localStorage.setItem('lastUsername', username);

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

    allItems   = data.items || [];
    activeTags = new Set();   // reset tag filter on each fresh load
    renderStats(data.stats, regions);
    buildTagFilters();
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

// ── Tag filter ──────────────────────────────────────────────────────────────

function normalizeTag(tag) {
  if (!tag) return tag;
  // Collapse all tool-type tags into a single "Tools" bucket
  if (/tool/i.test(tag)) return 'Tools';
  return tag;
}

function buildTagFilters() {
  const tags = [...new Set(allItems.map(i => normalizeTag(i.tag)).filter(Boolean))].sort();
  const wrap  = document.getElementById('tag-chips');
  const sec   = document.getElementById('tag-filter-section');

  wrap.innerHTML = '';
  if (!tags.length) { sec.style.display = 'none'; return; }

  for (const tag of tags) {
    const chip = document.createElement('button');
    chip.className   = 'tag-chip';
    chip.textContent = tag;
    chip.onclick     = () => {
      if (activeTags.has(tag)) {
        activeTags.delete(tag);
        chip.classList.remove('active');
      } else {
        activeTags.add(tag);
        chip.classList.add('active');
      }
      renderTable();
    };
    wrap.appendChild(chip);
  }
  sec.style.display = 'flex';
}

// ── Table ──────────────────────────────────────────────────────────────────

let effortPanelRow = null;
let effortItemId   = null;

function renderTable() {
  closeEffortPanel();
  const visible = activeTags.size
    ? allItems.filter(r => activeTags.has(normalizeTag(r.tag)))
    : allItems;

  const sorted = [...visible];
  sorted.sort((a, b) => {
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

  const HEX = '⬡';

  for (const r of sorted) {
    const tierTag    = r.tier >= 0 ? `T${r.tier}` : (r.tag || '—');
    const buyStr     = r.highest_buy != null ? `${HEX} ${r.highest_buy.toLocaleString()}` : '—';
    const qtyStr     = r.total_qty   != null ? r.total_qty.toLocaleString()   : '—';
    const scoreStr   = r.score       > 0     ? `${HEX} ${r.score.toLocaleString()}` : '—';
    const tr = document.createElement('tr');
    if (r.source === 'none') tr.classList.add('unobtainable');
    tr.style.cursor = 'pointer';
    tr.title = 'Click to view effort breakdown';
    tr.addEventListener('click', () => toggleEffortPanel(r, tr));
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

// ── Effort panel ───────────────────────────────────────────────────────────

function toggleEffortPanel(item, sourceTr) {
  if (effortItemId === item.id) { closeEffortPanel(); return; }
  closeEffortPanel();

  effortItemId = item.id;
  sourceTr.classList.add('effort-active');

  const panelTr = document.createElement('tr');
  panelTr.className = 'effort-panel-row';
  panelTr.innerHTML = `
    <td colspan="6">
      <div class="effort-panel" id="effort-panel">
        <div class="effort-loading">
          <div class="spinner" style="width:20px;height:20px;border-width:2px"></div>
          <span>Calculating effort for ${escHtml(item.name)}\u2026</span>
        </div>
      </div>
    </td>
  `;
  sourceTr.after(panelTr);
  effortPanelRow = panelTr;

  _doFetchEffort(item, 1);
}

function closeEffortPanel() {
  if (effortPanelRow) { effortPanelRow.remove(); effortPanelRow = null; }
  document.querySelectorAll('.effort-active').forEach(r => r.classList.remove('effort-active'));
  effortItemId = null;
}

function refetchEffort(itemId) {
  const qty  = parseFloat(document.getElementById('effort-qty-input')?.value) || 1;
  const item = allItems.find(i => String(i.id) === String(itemId));
  if (!item) return;
  const panel = document.getElementById('effort-panel');
  if (panel) panel.innerHTML = `
    <div class="effort-loading">
      <div class="spinner" style="width:20px;height:20px;border-width:2px"></div>
      <span>Calculating\u2026</span>
    </div>
  `;
  _doFetchEffort(item, qty);
}

async function _doFetchEffort(item, quantity) {
  const params = new URLSearchParams({ item_id: item.id, quantity });
  if (playerId) params.set('player_id', playerId);

  try {
    const res  = await fetch(`${API}/api/effort?${params}`);
    const data = await res.json();
    if (!effortPanelRow) return;
    const panel = document.getElementById('effort-panel');
    if (!panel) return;
    if (!res.ok || data.error) {
      panel.innerHTML = `<div style="color:var(--red);padding:12px">${escHtml(data.error || 'Failed to calculate effort.')}</div>`;
      return;
    }
    _renderEffortPanel(data, item);
  } catch (e) {
    const panel = document.getElementById('effort-panel');
    if (panel) panel.innerHTML = `<div style="color:var(--red);padding:12px">Failed to reach the server.</div>`;
  }
}

function _renderEffortPanel(data, item) {
  const panel = document.getElementById('effort-panel');
  if (!panel) return;

  const fmt  = n => (n > 0) ? Math.round(n).toLocaleString() : '—';
  const fmtT = n => (n > 0) ? (n / 60).toLocaleString(undefined, {maximumFractionDigits: 1}) + ' min' : '—';

  // Profession breakdown table
  const profs = data.by_profession || {};
  const profRows = Object.entries(profs).map(([prof, v]) => `
    <tr>
      <td>${escHtml(prof)}</td>
      <td class="num">${fmt(v.gathering_stamina)}</td>
      <td class="num">${fmtT(v.gathering_time)}</td>
      <td class="num">${fmt(v.crafting_stamina)}</td>
      <td class="num">${fmtT(v.crafting_time)}</td>
    </tr>`).join('');

  const profTable = profRows
    ? `<table class="effort-table">
        <thead><tr>
          <th>Profession</th>
          <th class="num">Gather Stam</th><th class="num">Gather Time</th>
          <th class="num">Craft Stam</th><th class="num">Craft Time</th>
        </tr></thead>
        <tbody>${profRows}</tbody>
       </table>`
    : `<div style="color:var(--fg-dim);font-size:12px">No effort data available.</div>`;

  // External ingredient costs
  const costs = data.ingredient_costs || {};
  const costEntries = Object.entries(costs);
  const costsHtml = costEntries.length
    ? `<div class="effort-section-title">External Inputs</div>
       <div class="effort-costs">${costEntries.map(([id, info]) =>
         `<span class="effort-cost-chip">${escHtml(info.name || id)} \xd7 ${Math.round(info.quantity).toLocaleString()}</span>`
       ).join('')}</div>`
    : '';

  // Production chain
  const chain = data.chain || [];
  const chainHtml = chain.length
    ? `<div class="effort-section-title">Production Chain</div>
       <div class="effort-chain">${chain.map(s => {
         const method = s.method === 'extraction' ? 'gather' : 'craft';
         const power  = s.tool_power ? ` (power ${s.tool_power})` : '';
         const qty    = s.quantity != null ? parseFloat(s.quantity.toFixed(1)).toLocaleString() : '?';
         return `<div class="chain-step">
           <span class="badge ${method}">${method}</span>
           <span>${escHtml(s.item_name)} \xd7 ${qty}</span>
           <span style="color:var(--fg-dim)">${escHtml(s.skill || '')}${escHtml(power)}</span>
         </div>`;
       }).join('')}</div>`
    : '';

  // Warnings
  const warnings = (data.warnings || []);
  const warnsHtml = warnings.length
    ? `<div style="color:var(--fg-dim);font-size:12px;margin-top:8px">${warnings.map(w => `\u26a0 ${escHtml(w)}`).join('<br>')}</div>`
    : '';

  panel.innerHTML = `
    <div class="effort-header">
      <span class="effort-title">Effort: ${escHtml(data.item_name)} \xd7 ${parseFloat(data.quantity).toLocaleString()}</span>
      <div class="effort-header-actions">
        <label style="font-size:12px;color:var(--fg-dim)">Qty:
          <input type="number" id="effort-qty-input" class="effort-qty-input" value="${data.quantity}" min="1" />
        </label>
        <button class="btn secondary" style="padding:4px 10px;font-size:12px"
          onclick="refetchEffort('${escHtml(String(item.id))}')">\u21bb Recalc</button>
        <a href="https://bitjita.com/market/item/${escHtml(String(item.id))}" target="_blank"
           class="btn secondary" style="padding:4px 10px;font-size:12px;text-decoration:none">\u2197 bitjita</a>
        <button class="btn secondary" style="padding:4px 10px;font-size:12px" onclick="closeEffortPanel()">\u2715</button>
      </div>
    </div>
    ${profTable}
    ${costsHtml}
    ${chainHtml}
    ${warnsHtml}
  `;
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

// Restore last username from localStorage
const _savedUser = localStorage.getItem('lastUsername');
if (_savedUser) document.getElementById('username-input').value = _savedUser;

// ── Version badge ───────────────────────────────────────────────────────────

function timeAgo(isoStr) {
  if (!isoStr) return null;
  const diff = Math.floor((Date.now() - new Date(isoStr)) / 1000);
  if (diff < 60)   return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

async function loadVersion() {
  try {
    const res  = await fetch(`${API}/api/version`);
    const data = await res.json();
    const badge = document.getElementById('version-badge');

    const shaLine = `<span title="${data.sha_full || data.sha}">
      deploy <span style="color:var(--accent2);font-family:monospace">${data.sha}</span>
      ${data.branch !== 'main' ? `<span style="color:var(--fg-dim)">(${data.branch})</span>` : ''}
    </span>`;

    const recipeAge = timeAgo(data.recipes_built_at);
    const recipeLine = recipeAge
      ? `<span title="${data.recipes_built_at}">recipes updated <span style="color:var(--green)">${recipeAge}</span></span>`
      : '';

    badge.innerHTML = shaLine + (recipeLine ? recipeLine : '');
  } catch (_) {
    // version badge is non-critical — silently ignore
  }
}

loadVersion();
