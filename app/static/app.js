const state = {
  meta: null,
  dashboard: null,
  charts: {},
  filters: { categories: [], accounts: [], currencies: [], types: [] },
  draftFilters: null,
  searchTimer: null,
  mappingResolver: null,
  mappingFile: null,
  defaultCurrency: 'INR',
};

const palette = ['#b8ff63', '#70d8ff', '#a99dff', '#ffb66d', '#86edac', '#ff7d8d', '#d9c678', '#7896ff'];
const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const compact = new Intl.NumberFormat('en-IN', { notation: 'compact', maximumFractionDigits: 1 });
const number = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 });

if (window.Chart) {
  Chart.defaults.color = '#737d8d';
  Chart.defaults.borderColor = 'rgba(255,255,255,.055)';
  Chart.defaults.font.family = 'Inter, ui-sans-serif, system-ui, sans-serif';
  Chart.defaults.font.size = 10;
}

function currencyCode() {
  const code = state.dashboard?.currency;
  return code && code !== 'MIXED' ? code : (state.filters.currencies[0] || state.defaultCurrency || 'INR');
}

function fmtMoney(value, currency = currencyCode(), digits = 0) {
  const v = Number(value || 0);
  if (currency === 'MIXED') return `${compact.format(v)} mixed`;
  try {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency', currency, maximumFractionDigits: digits, minimumFractionDigits: 0,
    }).format(v);
  } catch {
    return `${currency} ${number.format(v)}`;
  }
}

function moneyTick(value) {
  const code = currencyCode();
  let symbol = '';
  try {
    symbol = new Intl.NumberFormat('en-IN', { style: 'currency', currency: code, currencyDisplay: 'narrowSymbol', maximumFractionDigits: 0 })
      .formatToParts(0).find(p => p.type === 'currency')?.value || `${code} `;
  } catch {
    symbol = `${code} `;
  }
  return `${symbol}${compact.format(value)}`;
}

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
}
function escAttr(value) { return esc(value).replace(/`/g, '&#96;'); }

async function jsonFetch(url, opts) {
  const response = await fetch(url, opts);
  let body = null;
  try { body = await response.json(); } catch { body = await response.text(); }
  if (!response.ok) {
    const message = body?.detail || body?.error || (typeof body === 'string' ? body : `Request failed (${response.status})`);
    throw new Error(message);
  }
  return body;
}

function chartBase() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 420 },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#181d26', borderColor: 'rgba(255,255,255,.11)', borderWidth: 1,
        titleColor: '#fff', bodyColor: '#b3bbc8', padding: 10, cornerRadius: 9,
      },
    },
    scales: {
      x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } },
      y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,.045)' }, ticks: { callback: moneyTick } },
    },
  };
}

function destroyChart(name) {
  if (state.charts[name]) {
    state.charts[name].destroy();
    delete state.charts[name];
  }
}

function makeChart(name, elementId, config) {
  if (!window.Chart) return;
  destroyChart(name);
  state.charts[name] = new Chart($(elementId), config);
}

function selectedMonths() {
  const value = $('monthSelect').value;
  return !value || value === '__all__' ? [] : [value];
}

function buildParams() {
  const p = new URLSearchParams();
  selectedMonths().forEach(v => p.append('months', v));
  Object.entries(state.filters).forEach(([key, values]) => values.forEach(v => p.append(key, v)));
  return p;
}

function activeFilterCount() {
  return Object.values(state.filters).reduce((sum, values) => sum + values.length, 0);
}

async function loadMeta(preservePeriod = true) {
  const oldPeriod = preservePeriod ? $('monthSelect').value : '';
  state.meta = await jsonFetch('/api/meta');
  $('fileCountBadge').textContent = state.meta.imports.length;

  const period = $('monthSelect');
  period.innerHTML = '<option value="__all__">All imported months</option>';
  state.meta.months.forEach(m => {
    const option = document.createElement('option');
    option.value = m.value;
    option.textContent = m.label;
    period.appendChild(option);
  });
  if (oldPeriod && [...period.options].some(o => o.value === oldPeriod)) {
    period.value = oldPeriod;
  } else {
    period.value = state.meta.months.length > 1 ? '__all__' : (state.meta.months[0]?.value || '__all__');
  }

  // Remove filter values that disappeared after deleting a source file.
  for (const key of Object.keys(state.filters)) {
    const allowed = new Set(state.meta[key] || []);
    state.filters[key] = state.filters[key].filter(v => allowed.has(v));
  }

  // Never silently add currencies together. If multiple currencies exist and none is selected,
  // default to the currency of the latest import so totals stay meaningful.
  if ((state.meta.currencies || []).length > 1 && !state.filters.currencies.length) {
    const latest = state.meta.imports.find(i => i.currency)?.currency;
    state.filters.currencies = [latest || state.meta.currencies[0]];
  }

  renderFilterGroups();
  renderActiveFilters();
  renderImports();
}

function renderFilterGroups() {
  if (!state.meta) return;
  const groups = [
    ['types', 'Transaction type'],
    ['categories', 'Category'],
    ['accounts', 'Account / wallet'],
    ['currencies', 'Currency'],
  ];
  state.draftFilters = JSON.parse(JSON.stringify(state.filters));
  $('filterGroups').innerHTML = groups.map(([key, label]) => {
    const values = state.meta[key] || [];
    if (!values.length) return '';
    const rows = values.map(value => {
      const id = `filter-${key}-${safeId(value)}`;
      const checked = state.draftFilters[key].includes(value) ? 'checked' : '';
      return `<label class="check-row" for="${id}"><input id="${id}" type="checkbox" data-filter-key="${key}" value="${escAttr(value)}" ${checked}><span>${esc(displayValue(key, value))}</span></label>`;
    }).join('');
    return `<section class="filter-group"><div class="filter-group-title"><strong>${label}</strong><span>${values.length} discovered</span></div><div class="check-list">${rows}</div></section>`;
  }).join('');

  document.querySelectorAll('[data-filter-key]').forEach(input => input.addEventListener('change', () => {
    const key = input.dataset.filterKey;
    const values = new Set(state.draftFilters[key]);
    input.checked ? values.add(input.value) : values.delete(input.value);
    state.draftFilters[key] = [...values];
  }));
}

function displayValue(key, value) {
  if (key === 'types') return value.charAt(0).toUpperCase() + value.slice(1);
  return value;
}

function safeId(value) { return String(value).replace(/[^a-zA-Z0-9_-]+/g, '-').slice(0, 70); }

function renderActiveFilters() {
  const chips = [];
  Object.entries(state.filters).forEach(([key, values]) => values.forEach(value => {
    chips.push(`<span class="filter-chip">${esc(displayValue(key, value))}<button type="button" data-remove-filter="${key}" data-value="${escAttr(value)}" aria-label="Remove filter">×</button></span>`);
  }));
  $('activeFilters').innerHTML = chips.join('');
  $('filterCount').textContent = activeFilterCount();
  document.querySelectorAll('[data-remove-filter]').forEach(btn => btn.addEventListener('click', async () => {
    const key = btn.dataset.removeFilter;
    state.filters[key] = state.filters[key].filter(v => v !== btn.dataset.value);
    renderFilterGroups();
    renderActiveFilters();
    await refresh();
  }));
}

async function refresh() {
  const params = buildParams();
  state.dashboard = await jsonFetch(`/api/dashboard?${params.toString()}`);
  if (state.dashboard.empty) {
    renderEmpty();
    await loadTransactions();
    return;
  }
  renderDashboard(state.dashboard);
  await loadTransactions();
}

function renderEmpty() {
  $('periodText').textContent = 'No matching transactions — import a CSV or clear filters.';
  ['spendKpi', 'avgDayKpi', 'netKpi', 'medianKpi', 'concentrationKpi'].forEach(id => $(id).textContent = '—');
  $('categoryRanks').innerHTML = '<div class="empty-state"><strong>No spend data</strong>Import a file or clear filters.</div>';
  ['insightList', 'recurringList', 'anomalyList'].forEach(id => $(id).innerHTML = '<div class="empty-state">Nothing to show for this view.</div>');
  Object.keys(state.charts).forEach(destroyChart);
}

function renderDashboard(d) {
  const k = d.kpis;
  const periodLabel = `${d.period.start} → ${d.period.end}`;
  $('periodText').textContent = `${periodLabel} · ${number.format(k.expenseCount)} expenses · ${k.activeDays} active days`;
  $('spendKpi').textContent = fmtMoney(k.spend, d.currency);
  $('avgDayKpi').textContent = fmtMoney(k.avgDay, d.currency);
  $('netKpi').textContent = fmtMoney(k.netCashFlow, d.currency);
  $('medianKpi').textContent = fmtMoney(k.medianTransaction, d.currency);
  $('concentrationKpi').textContent = `${number.format(k.top5Concentration)}%`;
  $('spendSub').textContent = `${k.expenseCount} expenses · transfers excluded`;
  $('avgDaySub').textContent = `${k.noSpendDays} no-spend days`;
  $('incomeSub').textContent = `${fmtMoney(k.income, d.currency)} income · ${fmtMoney(k.transfers, d.currency)} transfers`;

  const lastMonth = d.monthly.at(-1);
  const pill = $('trendPill');
  pill.className = 'trend-pill neutral';
  if (lastMonth?.mom == null) {
    pill.textContent = d.monthly.length < 2 ? 'Add another month for MoM' : 'No prior month';
  } else {
    pill.textContent = `${lastMonth.mom > 0 ? '↑' : '↓'} ${Math.abs(lastMonth.mom)}% vs prior month`;
    pill.classList.add(lastMonth.mom > 0 ? 'positive' : 'negative');
  }

  renderDaily(d);
  renderCategoryRanks(d);
  renderMonthly(d);
  renderWeekday(d);
  renderTime(d);
  renderCategoryBar(d);
  renderCategoryTrend(d);
  renderInsights(d);
  renderRecurring(d);
  renderAnomalies(d);
}

function renderDaily(d) {
  const opt = chartBase();
  opt.scales.x.ticks.maxTicksLimit = 11;
  opt.plugins.tooltip.callbacks = { label: c => `${c.dataset.label}: ${fmtMoney(c.raw, d.currency)}` };
  makeChart('daily', 'dailyChart', {
    type: 'line',
    data: {
      labels: d.daily.map(x => x.label),
      datasets: [
        { label: 'Daily spend', data: d.daily.map(x => x.spend), borderColor: css('--accent'), backgroundColor: 'rgba(184,255,99,.055)', fill: true, tension: .34, borderWidth: 2, pointRadius: 0, pointHoverRadius: 3 },
        { label: '7-day average', data: d.daily.map(x => x.rolling7), borderColor: css('--violet'), borderDash: [5, 5], tension: .34, borderWidth: 1.3, pointRadius: 0, fill: false },
      ],
    }, options: opt,
  });
}

function renderCategoryRanks(d) {
  const rows = d.categories.slice(0, 8);
  if (!rows.length) {
    $('categoryRanks').innerHTML = '<div class="empty-state">No category field was present, so imported expenses are shown as Uncategorized.</div>';
    return;
  }
  const max = Math.max(...rows.map(x => x.spend), 1);
  $('categoryRanks').innerHTML = rows.map((x, i) => `
    <div class="cat-rank">
      <div class="cat-rank-top"><span class="cat-rank-name" title="${escAttr(x.category)}">${String(i + 1).padStart(2, '0')} · ${esc(x.category)}</span><span class="cat-rank-value">${fmtMoney(x.spend, d.currency)}</span></div>
      <div class="progress"><span style="width:${Math.max(3, x.spend / max * 100)}%"></span></div>
      <div class="cat-rank-meta"><span>${x.count} tx</span><span>${x.share}% of spend</span></div>
    </div>`).join('');
}

function renderMonthly(d) {
  const opt = chartBase();
  opt.plugins.tooltip.callbacks = { label: c => `${c.dataset.label}: ${fmtMoney(c.raw, d.currency)}` };
  makeChart('monthly', 'monthlyChart', {
    type: 'bar',
    data: { labels: d.monthly.map(x => x.label), datasets: [{ label: 'Monthly spend', data: d.monthly.map(x => x.spend), backgroundColor: 'rgba(184,255,99,.72)', borderRadius: 7, borderSkipped: false, maxBarThickness: 48 }] },
    options: opt,
  });
  const last = d.monthly.at(-1);
  $('momBadge').textContent = last?.mom == null ? 'Add more months' : `${last.mom > 0 ? '+' : ''}${last.mom}% vs prior month`;
  $('momBadge').style.color = last?.mom > 0 ? css('--orange') : last?.mom < 0 ? css('--green') : '';
}

function renderWeekday(d) {
  const opt = chartBase();
  opt.plugins.tooltip.callbacks = { label: c => fmtMoney(c.raw, d.currency) };
  makeChart('weekday', 'weekdayChart', {
    type: 'bar',
    data: { labels: d.weekdays.map(x => x.day), datasets: [{ data: d.weekdays.map(x => x.spend), backgroundColor: d.weekdays.map((_, i) => i >= 5 ? 'rgba(169,157,255,.67)' : 'rgba(112,216,255,.52)'), borderRadius: 6, borderSkipped: false }] },
    options: opt,
  });
}

function renderTime(d) {
  const opt = chartBase();
  opt.scales.x.grid.display = false;
  opt.plugins.tooltip.callbacks = { label: c => fmtMoney(c.raw, d.currency) };
  makeChart('time', 'timeChart', {
    type: 'bar',
    data: { labels: d.timeOfDay.map(x => x.bucket), datasets: [{ data: d.timeOfDay.map(x => x.spend), backgroundColor: ['rgba(112,216,255,.50)', 'rgba(184,255,99,.65)', 'rgba(169,157,255,.62)', 'rgba(255,182,109,.56)'], borderRadius: 7, borderSkipped: false }] },
    options: opt,
  });
}

function renderCategoryBar(d) {
  const rows = d.categories.slice(0, 10).reverse();
  const opt = chartBase();
  opt.indexAxis = 'y';
  opt.scales.x.grid.color = 'rgba(255,255,255,.04)';
  opt.scales.y.grid.display = false;
  opt.plugins.tooltip.callbacks = { label: c => fmtMoney(c.raw, d.currency) };
  makeChart('categoryBar', 'categoryBar', {
    type: 'bar',
    data: { labels: rows.map(x => x.category), datasets: [{ data: rows.map(x => x.spend), backgroundColor: rows.map((_, i) => i === rows.length - 1 ? 'rgba(184,255,99,.74)' : 'rgba(169,157,255,.42)'), borderRadius: 6, borderSkipped: false }] },
    options: opt,
  });
}

function renderCategoryTrend(d) {
  const months = d.monthly.map(x => x.month);
  const topCats = d.categories.slice(0, 6).map(x => x.category);
  const lookup = new Map(d.categoryByMonth.map(x => [`${x.month}|${x.category}`, x.spend]));
  const datasets = topCats.map((cat, i) => ({ label: cat, data: months.map(m => lookup.get(`${m}|${cat}`) || 0), backgroundColor: `${palette[i % palette.length]}99`, borderColor: palette[i % palette.length], borderWidth: 1, borderRadius: 4, borderSkipped: false }));
  const opt = chartBase();
  opt.scales.x.stacked = true;
  opt.scales.y.stacked = true;
  opt.plugins.legend = { display: true, position: 'bottom', labels: { boxWidth: 7, boxHeight: 7, usePointStyle: true, padding: 15, color: '#7f8897' } };
  opt.plugins.tooltip.callbacks = { label: c => `${c.dataset.label}: ${fmtMoney(c.raw, d.currency)}` };
  makeChart('categoryTrend', 'categoryTrendChart', { type: 'bar', data: { labels: d.monthly.map(x => x.label), datasets }, options: opt });
}

function renderInsights(d) {
  $('insightList').innerHTML = d.insights.map((x, i) => `<div class="insight-item"><div class="insight-icon">${['↗', '⌁', '◎', '∿', '○', '◷'][i % 6]}</div><div><strong>${esc(x.title)}</strong><p>${esc(x.text)}</p></div></div>`).join('') || '<div class="empty-state">Add more transactions to generate observations.</div>';
}

function renderRecurring(d) {
  $('recurringList').innerHTML = d.recurringPatterns.slice(0, 8).map((x, i) => `<div class="rank-item"><div class="rank-num">${String(i + 1).padStart(2, '0')}</div><div class="rank-main"><strong>${esc(x.label)}</strong><span>${x.count} appearances · avg ${fmtMoney(x.avg, d.currency)}</span></div><div class="rank-value">${fmtMoney(x.spend, d.currency)}</div></div>`).join('') || '<div class="empty-state">No repeated description patterns detected yet.</div>';
}

function renderAnomalies(d) {
  $('anomalyList').innerHTML = d.anomalies.slice(0, 8).map(x => `<div class="anomaly-item"><div class="anomaly-bar"></div><div class="anomaly-main"><strong title="${escAttr(x.notes || x.category)}">${esc(x.notes || x.category)}</strong><span>${esc(x.date)} · ${esc(x.category)}${x.multipleOfMedian ? ` · ${x.multipleOfMedian}× median` : ''}</span></div><div class="anomaly-amount">${fmtMoney(x.amount, d.currency)}</div></div>`).join('') || '<div class="empty-state">No statistical outliers detected.</div>';
}

async function loadTransactions() {
  const p = buildParams();
  const query = $('searchInput').value.trim();
  if (query) p.set('q', query);
  const data = await jsonFetch(`/api/transactions?${p.toString()}`);
  $('txCount').textContent = `${data.count} matching transaction${data.count === 1 ? '' : 's'}`;
  $('tableHint').textContent = data.shown < data.count ? `Showing latest ${data.shown}` : 'All matching rows';
  $('txBody').innerHTML = data.rows.map(r => {
    const sign = r.type === 'expense' ? '−' : r.type === 'income' ? '+' : r.type === 'transfer' ? '⇄' : '·';
    return `<tr>
      <td class="tx-date">${esc(r.date)}<small>${esc(r.time)}</small></td>
      <td><span class="type-pill type-${escAttr(r.type)}">${esc(r.type)}</span></td>
      <td>${esc(r.category)}</td>
      <td class="description" title="${escAttr(r.notes)}">${esc(r.notes || '—')}</td>
      <td>${esc(r.account)}</td>
      <td><span class="source-tag" title="${escAttr(r.source)}">${esc(r.source)}</span></td>
      <td class="right amount-${escAttr(r.type)}">${sign} ${fmtMoney(r.amount, r.currency)}</td>
    </tr>`;
  }).join('');
}

function openUpload() {
  $('uploadOverlay').classList.remove('hidden');
  $('uploadOverlay').setAttribute('aria-hidden', 'false');
  $('uploadStatus').textContent = '';
}
function closeUpload() {
  $('uploadOverlay').classList.add('hidden');
  $('uploadOverlay').setAttribute('aria-hidden', 'true');
}

async function uploadOne(file, mapping = null, fallbackCurrency = ($('importCurrency').value || state.defaultCurrency)) {
  const form = new FormData();
  form.append('file', file);
  form.append('default_currency', fallbackCurrency || 'INR');
  if (mapping) form.append('mapping_json', JSON.stringify(mapping));
  return jsonFetch('/api/import-file', { method: 'POST', body: form });
}

async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  openUpload();
  const fallback = ($('importCurrency').value || 'INR').trim().toUpperCase().slice(0, 3);
  if (!/^[A-Z]{3}$/.test(fallback)) {
    $('uploadStatus').textContent = 'Fallback currency must be a 3-letter code such as INR, USD or EUR.';
    return;
  }
  state.defaultCurrency = fallback;
  $('importCurrency').value = fallback;
  const messages = [];
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    $('uploadStatus').textContent = `Reading ${file.name} · ${i + 1} of ${files.length}…`;
    try {
      let result = await uploadOne(file);
      if (result.status === 'needs_mapping') {
        const mapped = await requestMapping(file, result.preview);
        if (!mapped) {
          messages.push(`${file.name}: skipped`);
          continue;
        }
        result = await uploadOne(file, mapped.mapping, mapped.currency);
      }
      if (result.status === 'duplicate') {
        messages.push(`${file.name}: already imported`);
      } else {
        const warning = result.warnings?.length ? ` · ${result.warnings.join(' ')}` : '';
        messages.push(`${file.name}: ${result.rows} rows imported${warning}`);
      }
    } catch (error) {
      messages.push(`${file.name}: ${error.message}`);
    }
    $('uploadStatus').textContent = messages.join('  •  ');
  }
  await loadMeta(false);
  await refresh();
  $('uploadStatus').textContent = messages.join('  •  ');
  if (messages.length && messages.every(x => !/skipped|error|could not|required/i.test(x))) {
    setTimeout(closeUpload, 1100);
  }
}

function requestMapping(file, preview) {
  state.mappingFile = file;
  $('mappingTitle').textContent = `Map ${file.name}`;
  $('mappingSubtitle').textContent = `Detected ${preview.columns.length} columns. Confirm the best matches below.`;
  const conf = Math.round((preview.confidence || 0) * 100);
  const ambiguous = preview.ambiguous?.length ? ` · ambiguous: ${preview.ambiguous.join(', ')}` : '';
  $('mappingConfidence').textContent = `Auto-detection confidence ${conf}%${ambiguous}. Only date and monetary fields are required.`;

  const fields = [
    ['mapDate', 'date'], ['mapAmount', 'amount'], ['mapDebit', 'debit'], ['mapCredit', 'credit'],
    ['mapType', 'type'], ['mapDescription', 'description'], ['mapCategory', 'category'],
    ['mapAccount', 'account'], ['mapCurrency', 'currency'],
  ];
  fields.forEach(([id, role]) => fillMappingSelect($(id), preview.columns, preview.mapping?.[role] || ''));
  $('fallbackCurrency').value = ($('importCurrency').value || state.defaultCurrency).trim().toUpperCase().slice(0, 3);
  renderPreview(preview.columns, preview.sample || []);
  $('mappingOverlay').classList.remove('hidden');
  $('mappingOverlay').setAttribute('aria-hidden', 'false');

  return new Promise(resolve => { state.mappingResolver = resolve; });
}

function fillMappingSelect(select, columns, chosen) {
  select.innerHTML = '<option value="">Not present</option>';
  columns.forEach(column => {
    const option = document.createElement('option');
    option.value = column;
    option.textContent = column;
    select.appendChild(option);
  });
  if (chosen && columns.includes(chosen)) select.value = chosen;
}

function renderPreview(columns, rows) {
  $('previewTable').innerHTML = `<thead><tr>${columns.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map(c => `<td>${esc(String(row[c] ?? '').slice(0, 80))}</td>`).join('')}</tr>`).join('')}</tbody>`;
}

function finishMapping(value) {
  $('mappingOverlay').classList.add('hidden');
  $('mappingOverlay').setAttribute('aria-hidden', 'true');
  const resolve = state.mappingResolver;
  state.mappingResolver = null;
  state.mappingFile = null;
  if (resolve) resolve(value);
}

function mappingValue() {
  const mapping = {
    date: $('mapDate').value,
    amount: $('mapAmount').value,
    debit: $('mapDebit').value,
    credit: $('mapCredit').value,
    type: $('mapType').value,
    description: $('mapDescription').value,
    category: $('mapCategory').value,
    account: $('mapAccount').value,
    currency: $('mapCurrency').value,
  };
  Object.keys(mapping).forEach(k => { if (!mapping[k]) delete mapping[k]; });
  if (!mapping.date) throw new Error('Choose a date/time column.');
  if (!mapping.amount && !mapping.debit && !mapping.credit) throw new Error('Choose an amount column or debit/credit columns.');
  const currency = ($('fallbackCurrency').value || 'INR').trim().toUpperCase().slice(0, 3);
  if (!/^[A-Z]{3}$/.test(currency)) throw new Error('Fallback currency must be a 3-letter code such as INR or USD.');
  state.defaultCurrency = currency;
  return { mapping, currency };
}

function openFilters() {
  renderFilterGroups();
  $('filterOverlay').classList.remove('hidden');
  $('filterOverlay').setAttribute('aria-hidden', 'false');
}
function closeFilters() {
  $('filterOverlay').classList.add('hidden');
  $('filterOverlay').setAttribute('aria-hidden', 'true');
}

function openManage() {
  renderImports();
  $('manageOverlay').classList.remove('hidden');
  $('manageOverlay').setAttribute('aria-hidden', 'false');
}
function closeManage() {
  $('manageOverlay').classList.add('hidden');
  $('manageOverlay').setAttribute('aria-hidden', 'true');
}

function renderImports() {
  if (!state.meta) return;
  $('importList').innerHTML = state.meta.imports.map(item => {
    const mapping = Object.entries(item.mapping || {}).map(([k, v]) => `<span class="mapping-tag">${esc(k)} → ${esc(v)}</span>`).join('');
    const warnings = (item.warnings || []).map(w => `<span class="warning-text">⚠ ${esc(w)}</span>`).join('');
    return `<div class="import-row"><div class="import-main"><strong>${esc(item.filename)}</strong><span>${esc(item.month_start || '—')} → ${esc(item.month_end || '—')} · ${item.row_count} rows · ${esc(item.currency || '')}</span><div class="mapping-tags">${mapping}</div>${warnings}</div><button class="delete-btn" data-import-id="${item.id}">Delete</button></div>`;
  }).join('') || '<div class="empty-state"><strong>No files imported</strong>Import a transaction CSV to begin.</div>';

  document.querySelectorAll('.delete-btn').forEach(btn => btn.addEventListener('click', async () => {
    if (!confirm('Delete this imported file and all transactions created from it?')) return;
    await jsonFetch(`/api/imports/${btn.dataset.importId}`, { method: 'DELETE' });
    await loadMeta(false);
    await refresh();
    renderImports();
  }));
}

// Header navigation
[...document.querySelectorAll('.nav-link')].forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.nav-link').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  $(btn.dataset.scroll).scrollIntoView({ behavior: 'smooth', block: 'start' });
}));

$('uploadBtn').addEventListener('click', openUpload);
$('chooseFileBtn').addEventListener('click', () => $('fileInput').click());
$('closeUploadBtn').addEventListener('click', closeUpload);
$('fileInput').addEventListener('change', e => { uploadFiles(e.target.files); e.target.value = ''; });
$('manageBtn').addEventListener('click', openManage);
$('closeManageBtn').addEventListener('click', closeManage);
$('filterBtn').addEventListener('click', openFilters);
$('tableFilterBtn').addEventListener('click', openFilters);
$('closeFilterBtn').addEventListener('click', closeFilters);
$('monthSelect').addEventListener('change', refresh);
$('searchInput').addEventListener('input', () => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(loadTransactions, 220);
});

$('applyFiltersBtn').addEventListener('click', async () => {
  state.filters = JSON.parse(JSON.stringify(state.draftFilters || state.filters));
  // If several currencies were selected, allow it but the dashboard will explicitly show MIXED.
  renderActiveFilters();
  closeFilters();
  await refresh();
});
$('clearFiltersBtn').addEventListener('click', () => {
  state.draftFilters = { categories: [], accounts: [], currencies: [], types: [] };
  document.querySelectorAll('[data-filter-key]').forEach(input => { input.checked = false; });
});

$('confirmMappingBtn').addEventListener('click', () => {
  try { finishMapping(mappingValue()); }
  catch (error) { $('mappingConfidence').textContent = error.message; $('mappingConfidence').style.color = css('--orange'); }
});
$('cancelMappingBtn').addEventListener('click', () => finishMapping(null));
$('skipMappingBtn').addEventListener('click', () => finishMapping(null));

const dropZone = $('dropZone');
['dragenter', 'dragover'].forEach(eventName => dropZone.addEventListener(eventName, event => {
  event.preventDefault();
  dropZone.classList.add('dragging');
}));
['dragleave', 'drop'].forEach(eventName => dropZone.addEventListener(eventName, event => {
  event.preventDefault();
  dropZone.classList.remove('dragging');
}));
dropZone.addEventListener('drop', event => uploadFiles(event.dataTransfer.files));

$('uploadOverlay').addEventListener('click', e => { if (e.target === $('uploadOverlay')) closeUpload(); });
$('manageOverlay').addEventListener('click', e => { if (e.target === $('manageOverlay')) closeManage(); });
$('filterOverlay').addEventListener('click', e => { if (e.target === $('filterOverlay')) closeFilters(); });

(async function init() {
  try {
    await loadMeta(false);
    await refresh();
  } catch (error) {
    console.error(error);
    $('periodText').textContent = `Could not load the backend: ${error.message}`;
  }
})();
