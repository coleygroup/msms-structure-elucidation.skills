/* Copyright (c) 2026 Coley Group. */
'use strict';
let STATIC = window.MSMS_STATIC || null;  // embedded export data, or loaded from MSMS_STATIC_SRC at start
const DEMO = () => Boolean(STATIC && STATIC.demo_reviews);  // exported page whose reviews stay in this browser
const READONLY = () => Boolean(STATIC && !STATIC.demo_reviews);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d));
const pct = v => (v == null ? '—' : `${(100 * Number(v)).toFixed(0)}%`);
const OUTCOME = { atlas: 'ICEBERG Atlas', model: 'Model prediction', 'no-energy-pair': 'No Atlas energy within 2 eV',
  'not-in-atlas': 'Not in ICEBERG Atlas', 'atlas-failed': 'Atlas retrieval failed', 'no-formula': 'No formula',
  'no-pubchem-formula': 'No PubChem formula' };
const outcomeLabel = r => (r.outcome === 'model' ? `${r.top_model || 'Model'} prediction` : OUTCOME[r.outcome]);
const modelLabel = c => (String(c.source).startsWith('public') ? null : `${c.model_name || c.source}${c.model_version ? ' ' + c.model_version : ''}`);
const DECISION = { keep: 'Kept', uncertain: 'Uncertain', reject: 'Rejected' };
const ICON = { keep: '✓', uncertain: '?', reject: '✕' };

const S = {
  index: [], order: [], token: null, sort: { key: 'label', dir: 1 },
  r: null, result: null, notes: null, cand: 0, pairIdx: 0, compare: null, peak: null, zoom: null,
  payloads: new Map(), thumbs: new Map(), drafts: new Map(), saveTimers: {}, loading: 0, formulaCache: {},
};

/* ---------- data access: live server or embedded static export ---------- */
async function getJSON(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${url} failed (${response.status})`);
  return data;
}
const api = {
  async index() { return STATIC ? { results: STATIC.index } : getJSON('/api/index'); },
  async state(i) {
    if (!STATIC) return getJSON(`/api/state?result=${i}`);
    const r = STATIC.results[i];
    return { result: r.result, notes: DEMO() ? demoNotes(i) : r.notes, label: r.label, result_index: i };
  },
  async candidate(i, c) {
    if (STATIC) return STATIC.results[i].structures[c] || { svg: '', fragments: {}, peaks: {} };
    return getJSON(`/api/candidate/${i}/${c}`);
  },
  async thumb(i) {
    if (STATIC) return STATIC.results[i].structures[0]?.svg || '';
    const response = await fetch(`/api/thumb/${i}`);
    return response.ok ? response.text() : '';
  },
  async save(body) {
    if (DEMO()) return demoSave(body);
    return getJSON('/api/review', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Review-Token': S.token }, body: JSON.stringify(body) });
  },
};

/* ---------- demo reviews: kept in this browser only ---------- */
const demoKey = () => `msms-demo-reviews:${STATIC.exported_at}`;
function demoStore() {
  try { return JSON.parse(localStorage.getItem(demoKey()) || '{}'); } catch (e) { return {}; }
}
function demoNotes(i) {
  const stored = demoStore()[i];
  return stored || JSON.parse(JSON.stringify(STATIC.results[i].notes || { version: 1, candidates: {} }));
}
function reviewSummary(notes, keys) {
  const entries = notes.candidates || {};
  const reviewed = Object.values(entries).filter(n => (n.decision || 'unreviewed') !== 'unreviewed' || n.comment || Object.keys(n.fragments || {}).length).length;
  for (const decision of ['keep', 'uncertain', 'reject']) {
    const ranks = Object.entries(entries).filter(([k, n]) => n.decision === decision && keys.includes(k)).map(([k]) => keys.indexOf(k) + 1);
    if (ranks.length) return { reviewed, verdict: decision, rank: Math.min(...ranks) };
  }
  return { reviewed, verdict: null, rank: null };
}
async function demoSave(body) {
  const i = body.result, notes = demoNotes(i);
  const entry = notes.candidates[body.candidate_key] ||= { fragments: {} };
  Object.assign(entry, { decision: body.decision, comment: body.comment, updated_at: new Date().toISOString() });
  if (body.fragment) {
    const key = `${body.fragment.ce}:${body.fragment.peak_index}`;
    entry.fragments ||= {};
    if (body.fragment.comment) entry.fragments[key] = { ce: body.fragment.ce, peak_index: body.fragment.peak_index, comment: body.fragment.comment };
    else delete entry.fragments[key];
  }
  const store = demoStore(); store[i] = notes;
  try { localStorage.setItem(demoKey(), JSON.stringify(store)); } catch (e) { /* still kept for this visit */ }
  const review = reviewSummary(notes, STATIC.results[i].result.candidates.map(c => c.review_key));
  return { saved: true, notes, review, reviewed: review.reviewed };
}
async function fetchData(src) {
  const response = await fetch(src);
  if (!response.ok) throw new Error(`${src} (${response.status})`);
  let bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes[0] === 0x1f && bytes[1] === 0x8b) {  // gzip, unless the host already decoded it
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
    bytes = new Uint8Array(await new Response(stream).arrayBuffer());
  }
  return JSON.parse(new TextDecoder().decode(bytes));
}
async function loadStatic() {
  // One data file, or several parts whose `results` are concatenated in order (for per-file size limits).
  if (STATIC || !window.MSMS_STATIC_SRC) return;
  const parts = await Promise.all([].concat(window.MSMS_STATIC_SRC).map(fetchData));
  STATIC = parts[0];
  for (const part of parts.slice(1)) STATIC.results = STATIC.results.concat(part.results);
}

/* ---------- chemistry helpers ---------- */
function hill(formula) {
  if (!formula) return '';
  const charge = (formula.match(/[+-]+$/) || [''])[0];
  const counts = {};
  for (const [, el, n] of formula.replace(/[+-]+$/, '').matchAll(/([A-Z][a-z]?)(\d*)/g)) counts[el] = (counts[el] || 0) + (n ? Number(n) : 1);
  const els = Object.keys(counts);
  const order = counts.C ? ['C', 'H', ...els.filter(e => e !== 'C' && e !== 'H').sort()] : els.sort();
  return order.filter(e => counts[e]).map(e => e + (counts[e] > 1 ? counts[e] : '')).join('') + charge;
}
const fhtml = f => esc(hill(f)).replace(/(\d+)/g, '<sub>$1</sub>').replace(/([+-])$/, '<sup>$1</sup>');
const ppm = (a, b) => ((a - b) / b) * 1e6;

/* ---------- overview ---------- */
function reviewState(r) { return r.review?.verdict || (r.review?.reviewed ? 'touched' : 'todo'); }
function filtered() {
  const q = $('f-q').value.trim().toLowerCase(), outcome = $('f-outcome').value, review = $('f-review').value, min = Number($('f-sim').value);
  const rows = S.index.filter(r => {
    if (outcome === 'none' ? r.candidates > 0 : outcome && r.outcome !== outcome) return false;
    if (review === 'todo' ? r.review?.reviewed : review && r.review?.verdict !== review) return false;
    if (min > 0 && !(r.top_similarity >= min)) return false;
    if (q && ![r.label, r.formula, fmt(r.parentmass, 4), r.top_smiles, ...(r.hypotheses || [])].join(' ').toLowerCase().includes(q)) return false;
    return true;
  });
  const { key, dir } = S.sort;
  const val = r => key === 'verdict' ? ['keep', 'uncertain', 'reject', 'touched', 'todo'].indexOf(reviewState(r)) : r[key];
  const cmp = (a, b) => {
    const x = val(a), y = val(b);
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (typeof x === 'string' ? x.localeCompare(y, undefined, { numeric: true }) : x - y) * dir;
  };
  return rows.sort(cmp);
}
function renderOverview() {
  const n = S.index.length, withC = S.index.filter(r => r.candidates), sims = withC.map(r => r.top_similarity);
  const reviewed = S.index.filter(r => r.review?.reviewed).length;
  const kpis = [[n, 'unknowns'], [withC.length, 'with ranked candidates'], [S.index.filter(r => r.outcome === 'model').length, `via ${[...new Set(S.index.filter(r => r.outcome === 'model').map(r => r.top_model || 'model'))].join(' / ') || 'model'} prediction`],
    [sims.filter(s => s >= 0.7).length, 'rank-1 similarity ≥ 0.7'], [sims.filter(s => s < 0.5).length, 'rank-1 below 0.5 (review)'],
    [`${reviewed}/${n}`, 'reviewed']];
  $('kpis').innerHTML = kpis.map(([v, l]) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join('');
  renderHist(sims);
  const counts = new Map();
  Object.keys(OUTCOME).forEach(k => S.index.filter(r => r.outcome === k).forEach(r => counts.set(outcomeLabel(r), (counts.get(outcomeLabel(r)) || 0) + 1)));
  const max = Math.max(1, ...counts.values());
  $('outcomes').innerHTML = [...counts].map(([label, n]) =>
    `<div class="hbar"><span>${esc(label)}</span><span class="track"><span class="fill" style="width:${(100 * n / max).toFixed(1)}%"></span></span><span class="n">${n}</span></div>`).join('');
  const models = [...new Set(S.index.filter(r => r.outcome === 'model').map(r => r.top_model || 'Model'))];
  $('f-outcome').querySelector('option[value="model"]').textContent = models.length ? `${models.join(' / ')} prediction` : 'Model prediction';
  const rv = { keep: 0, uncertain: 0, reject: 0, touched: 0, todo: 0 }; S.index.forEach(r => rv[reviewState(r)]++);
  const segs = [['keep', 'var(--good)'], ['uncertain', 'var(--warn)'], ['reject', 'var(--bad)'], ['touched', 'var(--text-3)']];
  $('review-sub').textContent = `${reviewed} of ${n} unknowns have notes`;
  $('review-bar').innerHTML = `<div class="stack" role="img" aria-label="Review progress">${segs.filter(([k]) => rv[k]).map(([k, c]) => `<span style="width:${100 * rv[k] / n}%;background:${c}" title="${k}: ${rv[k]}"></span>`).join('')}</div>
    <div class="stack-legend">${[['keep', 'Kept'], ['uncertain', 'Uncertain'], ['reject', 'Rejected'], ['touched', 'Notes only'], ['todo', 'Not reviewed']]
      .map(([k, l]) => `<span>${ICON[k] ? `<span class="ico ${k}">${ICON[k]}</span>` : ''} ${l} <b>${rv[k]}</b></span>`).join('')}</div>`;
  renderTable();
}
function renderHist(sims) {
  const bins = Array.from({ length: 20 }, (_, i) => ({ lo: i / 20, n: 0 }));
  sims.forEach(s => { bins[Math.min(19, Math.floor(s * 20))].n++; });
  const W = 640, H = 210, m = { l: 34, r: 8, t: 22, b: 26 }, iw = W - m.l - m.r, ih = H - m.t - m.b, bw = iw / 20;
  const ymax = Math.max(1, ...bins.map(b => b.n)), y = v => m.t + ih - (v / ymax) * ih, step = ymax > 24 ? 10 : 5;
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Histogram of rank-1 similarity">`;
  for (let v = 0; v <= ymax; v += step) s += `<g class="axis"><line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text></g>`;
  bins.forEach((b, i) => {
    const x = m.l + i * bw + 1, h = m.t + ih - y(b.n), w = bw - 2;
    if (b.n) s += `<path d="M${x},${m.t + ih}v${-Math.max(0, h - 3)}q0,-3 3,-3h${w - 6}q3,0 3,3v${Math.max(0, h - 3)}z" fill="var(--accent)"/>`;
    s += `<rect x="${m.l + i * bw}" y="${m.t}" width="${bw}" height="${ih}" fill="transparent" data-tip="Similarity ${b.lo.toFixed(2)}–${(b.lo + 0.05).toFixed(2)}<br><b>${b.n}</b> unknowns"/>`;
  });
  for (let v = 0; v <= 10; v += 2) s += `<g class="axis"><text x="${m.l + v * 2 * bw}" y="${H - 8}" text-anchor="middle">${(v / 10).toFixed(1)}</text></g>`;
  const x5 = m.l + 10 * bw;
  s += `<line class="guide" x1="${x5}" x2="${x5}" y1="${m.t - 10}" y2="${m.t + ih}"/><text class="side-label" x="${x5 + 4}" y="${m.t - 12}">review below 0.5</text></svg>`;
  $('hist').innerHTML = s;
}
let thumbObserver = null;
const thumbQueue = []; let thumbActive = 0;
function pumpThumbs() {
  while (thumbActive < 6 && thumbQueue.length) {
    const [i, el] = thumbQueue.shift(); thumbActive++;
    api.thumb(i).then(svg => { S.thumbs.set(i, svg); el.innerHTML = svg; }).catch(() => {}).finally(() => { thumbActive--; pumpThumbs(); });
  }
}
function renderTable() {
  S.order = filtered().map(r => r.index);
  const rows = S.order.map(i => S.index[i]);
  $('ov-count').textContent = `${rows.length} of ${S.index.length}`;
  document.querySelectorAll('#ov-table th[data-k]').forEach(th => th.setAttribute('aria-sort', th.dataset.k === S.sort.key ? (S.sort.dir > 0 ? 'ascending' : 'descending') : 'none'));
  $('ov-table').tBodies[0].innerHTML = rows.map(r => {
    const kind = r.candidates ? r.outcome : 'none', st = reviewState(r);
    return `<tr data-i="${r.index}" tabindex="0">
      <td><b>${esc(r.label)}</b></td><td class="num">${fmt(r.parentmass, 4)}</td><td class="num">${fmt(r.rt_min, 2)}</td>
      <td>${r.formula ? fhtml(r.formula) : '<span class="sub">—</span>'}</td>
      <td><span class="chip"><span class="outcome-ico ${kind}"></span><span>${esc(outcomeLabel(r))}</span></span></td>
      <td>${r.top_smiles ? `<span class="thumb" data-thumb="${r.index}">${S.thumbs.get(r.index) || ''}</span>` : ''}</td>
      <td class="num">${r.candidates ? `<span class="simcell">${fmt(r.top_similarity, 3)}<span class="minibar"><i style="width:${100 * r.top_similarity}%"></i></span></span>` : '—'}</td>
      <td class="num">${pct(r.top_explained)}</td>
      <td>${DECISION[st] ? `<span class="chip"><span class="ico ${st}">${ICON[st]}</span>${DECISION[st]}${r.review.rank ? ` #${r.review.rank}` : ''}</span>` : st === 'touched' ? '<span class="chip">Notes</span>' : '<span class="sub">—</span>'}</td></tr>`;
  }).join('');
  thumbObserver?.disconnect();
  thumbObserver = new IntersectionObserver(entries => entries.forEach(e => {
    if (!e.isIntersecting) return;
    const el = e.target, i = Number(el.dataset.thumb); thumbObserver.unobserve(el);
    if (!S.thumbs.has(i)) { thumbQueue.push([i, el]); pumpThumbs(); }
  }), { rootMargin: '200px' });
  document.querySelectorAll('[data-thumb]').forEach(el => { if (!S.thumbs.has(Number(el.dataset.thumb))) thumbObserver.observe(el); });
}

/* ---------- workspace ---------- */
const cand = (k = S.cand) => S.result.candidates[k];
const pairOf = c => (c.energy_alignment || [])[S.pairIdx] || (c.energy_alignment || [])[0] || null;
const noteFor = c => S.notes?.candidates?.[c.review_key] || {};
function nearest(peaks, mz) {
  let best = -1, d = 0.01;
  peaks.forEach((p, i) => { const x = Math.abs(p[0] - mz); if (x < d) { d = x; best = i; } });
  return best;
}
function spectraFor(c) {
  const pair = pairOf(c);
  if (!pair) return null;
  const exp = S.result.spectra[pair.experimental_key] || [], pred = c.predicted_spectra?.[pair.atlas_key] || [];
  const ids = c.predicted_fragment_ids?.[pair.atlas_key] || [];
  const precursor = Number(S.result.parentmass) || Infinity;
  const expMax = Math.max(1e-9, ...exp.filter(p => p[0] <= precursor + 2).map(p => p[1]));
  const predMax = Math.max(1e-9, ...pred.map(p => p[1]));
  const e2p = new Map(), p2e = new Map();
  for (const m of (c.matched_peaks || []).filter(m => String(m.ce) === String(pair.experimental_key))) {
    const ei = nearest(exp, m.mz), pi = nearest(pred, m.predicted_mz);
    if (ei >= 0 && pi >= 0) { e2p.set(ei, pi); if (!p2e.has(pi) || exp[ei][1] > exp[p2e.get(pi)][1]) p2e.set(pi, ei); }
  }
  return { pair, exp, pred, ids, expMax, predMax, e2p, p2e, precursor };
}
function defaultRange(sp) {
  const mzs = [...sp.pred.map(p => p[0]), ...sp.exp.filter(p => p[0] <= sp.precursor + 2).map(p => p[0])];
  const lo = Math.max(0, Math.min(...mzs) - 12), hi = Math.max(sp.precursor === Infinity ? 0 : sp.precursor, ...mzs) + 12;
  return [Math.floor(lo / 10) * 10, Math.ceil(hi / 10) * 10];
}
function payload(k) {
  const key = `${S.r}:${k}`;
  if (!S.payloads.has(key)) {
    const p = api.candidate(S.r, k).catch(() => ({ svg: '', fragments: {}, peaks: {} }));
    p.then(v => { p.value = v; });
    S.payloads.set(key, p);
  }
  return S.payloads.get(key);
}

async function openResult(i) {
  if (S.r === i && S.result) { renderWorkspace(); return; }
  const token = ++S.loading;
  $('ws-name').textContent = S.index[i]?.label || 'Loading…';
  try {
    const data = await api.state(i);
    if (token !== S.loading) return;
    S.r = i; S.result = data.result; S.notes = data.notes || { version: 1, candidates: {} };
    if (data.review_token) S.token = data.review_token;
    S.cand = 0; S.pairIdx = 0; S.compare = null; S.peak = null; S.zoom = null; S.formulaCache = {};
    renderWorkspace();
    S.result.candidates.slice(0, 12).forEach((_, k) => payload(k).then(p => { if (S.r === i) fillMini(k, p); }));
  } catch (error) { showError(`Cannot load ${S.index[i]?.label || 'result'}: ${error.message}`); }
}
function renderWorkspace() {
  showError('');
  const r = S.index[S.r], res = S.result, has = res.candidates.length > 0;
  const pos = S.order.indexOf(S.r);
  $('ws-name').textContent = r.label;
  $('ws-pos').textContent = pos >= 0 ? `${pos + 1} / ${S.order.length}` : `— / ${S.order.length}`;
  const energies = (res.energy_mapping || []).map(m => `${fmt(m.input_value, 0)} ${esc(m.input_unit)}`).join(', ');
  $('ws-meta').innerHTML = [`m/z <b>${fmt(res.parentmass, 4)}</b>`, r.rt_min != null ? `RT <b>${fmt(r.rt_min, 2)} min</b>` : '',
    `${esc(res.adduct || '')}`, res.formula ? `<b>${fhtml(res.formula)}</b> <span class="sub">${esc(res.formula_source || '')}</span>` : 'no formula',
    energies ? `collision <b>${energies}</b>` : '', `<span class="chip"><span class="outcome-ico ${has ? r.outcome : 'none'}"></span>${esc(outcomeLabel(r))}</span>`]
    .filter(Boolean).map(x => `<span>${x}</span>`).join('');
  $('ws-grid').hidden = !has; $('verdict').hidden = !has; $('ws-empty').hidden = has;
  if (!has) { renderEmpty(); return; }
  renderRail(); renderEnergy(); renderPlots(); renderEvidence(); renderInspector(); renderVerdict();
}
function renderEmpty() {
  const res = S.result, r = S.index[S.r];
  const cmd = kind => `msms-structure-elucidation review --result ${esc(r.path)} --proposals ${kind} --model iceberg`;
  const why = {
    'no-formula': 'MSBuddy proposed no molecular formula and PubChem was not searched. Rerun retrieval without <code>--no-pubchem</code> so formulas of PubChem structures matching the precursor mass are tried.',
    'no-pubchem-formula': 'Neither MSBuddy nor a PubChem mass search found a formula for this precursor. Review the spectrum (adduct, in-source fragment, isotope, noise) and decide whether to generate structures de novo with FRIGID (<code>msms-denovo</code>).',
    'not-in-atlas': `${fhtml(res.formula)} has PubChem structures but no ICEBERG Atlas entry. Predict them with ICEBERG:<br><code>${cmd('pubchem')}</code>`,
    'no-energy-pair': `The ICEBERG Atlas has ${res.library_structures} structures for ${fhtml(res.formula)}, but none at a collision energy within 2 eV of the experiment. Predict them at the experimental energy with ICEBERG:<br><code>${cmd('atlas')}</code>`,
    'atlas-failed': 'Atlas retrieval did not complete (download or ranking error). Rerun retrieval for this unknown.',
  }[r.outcome] || 'No ranked candidates are available.';
  const hyps = (res.formula_hypotheses || []).map(h => h.formula).filter(f => f && f !== res.formula);
  $('ws-empty').innerHTML = `<h2>No ranked candidates</h2><p>${why}</p>
    ${hyps.length ? `<p>Other formula hypotheses — rerun retrieval with one of them:</p>${hypList(hyps)}` : ''}
    ${(res.warnings || []).map(w => `<p class="warn">⚠ ${esc(String(w).split('\n')[0])}</p>`).join('')}`;
}
function weightsLabel(checkpoint) {
  // Common directory of the checkpoint files, shortened to its last few parts.
  const parts = [].concat(checkpoint).map(p => String(p).split('/'));
  const common = [];
  for (let i = 0; parts.every(p => i < p.length - 1 && p[i] === parts[0][i]); i++) common.push(parts[0][i]);
  const dir = (common.length ? common : parts[0].slice(0, -1)).filter(x => x && x !== 'ckpt');
  return dir.slice(-3).join('/');
}
function hypList(hyps) {
  return `<div class="hyps">${hyps.map(f => `<div class="hyp"><span class="chip"><span>${fhtml(f)}</span></span><button class="copy" type="button" data-copy-formula="${esc(f)}" title="Copy a retrieval command that uses this formula">Copy run command</button></div>`).join('')}</div>`;
}
function fillMini(k, p) {
  const el = document.querySelector(`[data-mini="${k}"]`);
  if (el && p?.svg && !el.firstChild) el.innerHTML = p.svg;
}
function renderRail() {
  const res = S.result;
  $('rail-count').textContent = `${res.candidates.length}${STATIC ? ' shown' : ''}`;
  $('cands').innerHTML = res.candidates.map((c, k) => {
    const d = (S.drafts.get(`${S.r}|${c.review_key}`) || {}).decision || noteFor(c).decision;
    return `<li><button class="cand" type="button" data-k="${k}" aria-current="${k === S.cand}">
      <span class="rank">#${k + 1}</span><span class="mini" data-mini="${k}"></span>
      <span class="bars">
        <span class="row">sim <span class="minibar"><i style="width:${100 * c.entropy_similarity}%"></i></span><b>${fmt(c.entropy_similarity, 3)}</b></span>
        <span class="row">expl <span class="minibar"><i style="width:${100 * c.explained_intensity}%"></i></span><b>${pct(c.explained_intensity)}</b></span>
        <span class="tags">${modelLabel(c) ? `<span class="chip" title="${esc(modelLabel(c))} prediction">${esc(modelLabel(c))}</span>` : ''}${ICON[d] ? `<span class="chip"><span class="ico ${d}">${ICON[d]}</span>${DECISION[d]}</span>` : ''}${S.compare === k ? '<span class="chip">compared</span>' : ''}</span>
      </span></button></li>`;
  }).join('');
  res.candidates.forEach((_, k) => { const p = S.payloads.get(`${S.r}:${k}`); if (p) p.then(v => fillMini(k, v)); });
}
function renderEnergy() {
  const c = cand(), sel = $('energy');
  sel.innerHTML = (c.energy_alignment || []).map((a, i) => `<option value="${i}">${fmt(a.input_value, 0)} ${esc(a.input_unit)} ↔ ${fmt(a.atlas_ev, 0)} eV predicted</option>`).join('');
  sel.value = String(Math.min(S.pairIdx, Math.max(0, (c.energy_alignment || []).length - 1)));
  sel.disabled = (c.energy_alignment || []).length < 2;
  $('compare').setAttribute('aria-pressed', String(S.compare != null));
  $('compare').disabled = S.result.candidates.length < 2;
  const other = $('compare-with');
  other.hidden = S.compare == null;
  other.innerHTML = S.result.candidates.map((x, k) => k === S.cand ? '' : `<option value="${k}">vs #${k + 1} · ${fmt(x.entropy_similarity, 3)}</option>`).join('');
  if (S.compare != null) other.value = String(S.compare);
  $('legend').innerHTML = `<span><span class="sw" style="background:var(--exp)"></span>Experimental, matched</span>
    <span><span class="sw" style="background:var(--exp-soft)"></span>Experimental, unexplained</span>
    <span><span class="sw" style="background:var(--pred)"></span>Predicted, with fragment</span>
    <span><span class="sw" style="background:var(--pred-soft)"></span>Predicted, no fragment ID</span>`;
}

/* ---------- mirror plot ---------- */
function renderPlots() {
  const holder = $('plots'); holder.innerHTML = '';
  const ks = S.compare != null ? [S.cand, S.compare] : [S.cand];
  const base = spectraFor(cand());
  if (!base) { holder.innerHTML = '<p class="sub">No experimental energy was paired with a prediction for this candidate.</p>'; return; }
  if (!S.zoom) S.zoom = defaultRange(base);
  ks.forEach(k => {
    const el = document.createElement('div'); el.className = 'plot'; holder.append(el);
    drawMirror(el, k, ks.length > 1);
  });
}
function niceStep(span) {
  const raw = span / 8, p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map(x => x * p).find(x => x >= raw) || raw;
}
function drawMirror(el, k, compact) {
  const c = cand(k), sp = spectraFor(c);
  if (!sp) { el.innerHTML = `<p class="sub">#${k + 1}: no paired energy.</p>`; return; }
  const W = Math.max(480, el.clientWidth || 800), H = compact ? 250 : 360, m = { l: 44, r: 12, t: 20, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b, mid = m.t + ih / 2, half = ih / 2 - 6;
  const [lo, hi] = S.zoom, x = mz => m.l + ((mz - lo) / (hi - lo)) * iw;
  const inRange = mz => mz >= lo && mz <= hi;
  const sel = S.peak && S.peak.k === k ? S.peak : null;
  const selPred = sel ? (sel.kind === 'p' ? sel.i : sp.e2p.get(sel.i)) : null;
  const selExp = sel ? (sel.kind === 'e' ? sel.i : sp.p2e.get(sel.i)) : null;
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Mirror plot, experimental versus predicted rank ${k + 1}">`;
  [1, 0.5].forEach(v => {
    [mid - v * half, mid + v * half].forEach(yy => { s += `<g class="axis"><line x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}"/><text x="${m.l - 6}" y="${yy + 4}" text-anchor="end">${v * 100}%</text></g>`; });
  });
  const step = niceStep(hi - lo);
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) s += `<g class="axis"><text x="${x(v)}" y="${H - 8}" text-anchor="middle">${+v.toFixed(2)}</text></g>`;
  if (sp.precursor !== Infinity && inRange(sp.precursor)) s += `<line class="guide" x1="${x(sp.precursor)}" x2="${x(sp.precursor)}" y1="${m.t}" y2="${m.t + ih}"/><text class="side-label" x="${x(sp.precursor) - 4}" y="${m.t - 6}" text-anchor="end">precursor</text>`;
  if (selPred != null && inRange(sp.pred[selPred][0])) s += `<line class="guide" x1="${x(sp.pred[selPred][0])}" x2="${x(sp.pred[selPred][0])}" y1="${m.t}" y2="${m.t + ih}"/>`;
  else if (selExp != null && inRange(sp.exp[selExp][0])) s += `<line class="guide" x1="${x(sp.exp[selExp][0])}" x2="${x(sp.exp[selExp][0])}" y1="${m.t}" y2="${m.t + ih}"/>`;
  // Unexplained peaks first so matched ones draw on top.
  const expOrder = sp.exp.map((p, i) => i).filter(i => inRange(sp.exp[i][0])).sort((a, b) => sp.e2p.has(a) - sp.e2p.has(b));
  for (const i of expOrder) {
    const [mz, it] = sp.exp[i], h = Math.min(1, it / sp.expMax) * half;
    s += `<line class="peak exp${sp.e2p.has(i) ? ' m' : ''}${i === selExp ? ' sel' : ''}" data-s="e" data-i="${i}" x1="${x(mz)}" x2="${x(mz)}" y1="${mid}" y2="${mid - Math.max(1, h)}"/>`;
  }
  const predOrder = sp.pred.map((p, i) => i).filter(i => inRange(sp.pred[i][0])).sort((a, b) => (sp.ids[a] != null) - (sp.ids[b] != null));
  for (const i of predOrder) {
    const [mz, it] = sp.pred[i], h = (it / sp.predMax) * half;
    s += `<line class="peak pred${sp.ids[i] != null ? ' a' : ''}${i === selPred ? ' sel' : ''}" data-s="p" data-i="${i}" x1="${x(mz)}" x2="${x(mz)}" y1="${mid}" y2="${mid + Math.max(1, h)}"/>`;
  }
  // Direct labels on the strongest matched experimental peaks, spaced apart.
  const placed = [];
  [...sp.e2p.keys()].filter(i => inRange(sp.exp[i][0])).sort((a, b) => sp.exp[b][1] - sp.exp[a][1]).slice(0, 10).forEach(i => {
    const px = x(sp.exp[i][0]);
    if (placed.some(q => Math.abs(q - px) < 46) || placed.length >= 5) return;
    placed.push(px);
    const h = Math.min(1, sp.exp[i][1] / sp.expMax) * half;
    s += `<text class="plabel" x="${px}" y="${Math.max(m.t + 8, mid - h - 5)}" text-anchor="middle">${sp.exp[i][0].toFixed(3)}</text>`;
  });
  if (selExp != null && inRange(sp.exp[selExp][0])) {
    const h = Math.min(1, sp.exp[selExp][1] / sp.expMax) * half;
    s += `<circle class="peak-cap exp" cx="${x(sp.exp[selExp][0])}" cy="${mid - Math.max(1, h)}" r="4"/>`;
  }
  if (selPred != null && inRange(sp.pred[selPred][0])) {
    const px = x(sp.pred[selPred][0]), h = (sp.pred[selPred][1] / sp.predMax) * half, formula = S.formulaCache[`${k}:${sp.pair.atlas_key}:${selPred}`];
    const text = `${sp.pred[selPred][0].toFixed(3)}${formula ? ' · ' + esc(hill(formula)) : ''}`;
    s += `<circle class="peak-cap pred" cx="${px}" cy="${mid + Math.max(1, h)}" r="4"/>`;
    if (h > half - 24) {  // tall peak: label beside the tip so it clears the caption and axis
      const right = px < m.l + iw * 0.7;
      s += `<text class="plabel sel" x="${px + (right ? 8 : -8)}" y="${mid + h - 2}" text-anchor="${right ? 'start' : 'end'}">${text}</text>`;
    } else s += `<text class="plabel sel" x="${px}" y="${mid + h + 16}" text-anchor="middle">${text}</text>`;
  }
  s += `<line class="baseline" x1="${m.l}" x2="${W - m.r}" y1="${mid}" y2="${mid}"/>`;
  s += `<text class="side-label" x="${m.l + 6}" y="${m.t + 10}">Experimental · ${fmt(sp.pair.input_value, 0)} ${esc(sp.pair.input_unit)}</text>`;
  s += `<text class="side-label" x="${m.l + 6}" y="${m.t + ih - 4}">Predicted #${k + 1} · ${fmt(sp.pair.atlas_ev, 0)} eV · similarity ${fmt(sp.pair.entropy_similarity ?? c.entropy_similarity, 3)}</text>`;
  s += '</svg>';
  el.innerHTML = s;
  wirePlot(el, k, sp, { m, iw, ih, mid, x, lo, hi });
}
function wirePlot(el, k, sp, g) {
  const svg = el.querySelector('svg'), tip = $('tip');
  const scale = e => { const r = svg.getBoundingClientRect(); return { px: (e.clientX - r.left) * (svg.viewBox.baseVal.width / r.width), py: (e.clientY - r.top) * (svg.viewBox.baseVal.height / r.height) }; };
  const toMz = px => g.lo + ((px - g.m.l) / g.iw) * (g.hi - g.lo);
  const pick = e => {
    const { px, py } = scale(e), side = py < g.mid ? 'e' : 'p', list = side === 'e' ? sp.exp : sp.pred;
    let best = null, bd = 8;
    list.forEach((p, i) => {
      if (p[0] < g.lo || p[0] > g.hi) return;
      const d = Math.abs(g.x(p[0]) - px);
      if (d < bd - 0.5 || (Math.abs(d - bd) <= 0.5 && best && p[1] > list[best.i][1])) { bd = d; best = { kind: side, i }; }
    });
    return best;
  };
  let drag = null, brush = null, hot = null;
  const setHot = h => {
    if (hot) svg.querySelector(`[data-s="${hot.kind}"][data-i="${hot.i}"]`)?.classList.remove('hot');
    hot = h;
    if (hot) svg.querySelector(`[data-s="${hot.kind}"][data-i="${hot.i}"]`)?.classList.add('hot');
  };
  svg.addEventListener('pointerdown', e => { if (e.button !== 0) return; drag = { px: scale(e).px }; svg.setPointerCapture(e.pointerId); });
  svg.addEventListener('pointermove', e => {
    if (drag) {
      const cur = scale(e).px, a = Math.max(g.m.l, Math.min(drag.px, cur)), b = Math.min(g.m.l + g.iw, Math.max(drag.px, cur));
      if (b - a > 4) {
        if (!brush) { brush = document.createElementNS('http://www.w3.org/2000/svg', 'rect'); brush.setAttribute('class', 'brush'); svg.append(brush); }
        brush.setAttribute('x', a); brush.setAttribute('width', b - a); brush.setAttribute('y', g.m.t); brush.setAttribute('height', g.ih);
        tip.hidden = true; return;
      }
    }
    const h = pick(e);
    setHot(h);
    if (!h) { tip.hidden = true; if (k === S.cand) previewFragment(null); return; }
    tip.innerHTML = peakTip(k, sp, h);
    tip.hidden = false; placeTip(e.clientX, e.clientY);
    if (k === S.cand) previewFragment(h.kind === 'p' ? h.i : sp.e2p.get(h.i), sp);
  });
  svg.addEventListener('pointerup', e => {
    if (!drag) return;
    const cur = scale(e).px, moved = Math.abs(cur - drag.px);
    if (moved > 6) {
      const a = toMz(Math.max(g.m.l, Math.min(drag.px, cur))), b = toMz(Math.min(g.m.l + g.iw, Math.max(drag.px, cur)));
      drag = null; brush?.remove(); brush = null;
      if (b - a > 0.5) { S.zoom = [a, b]; renderPlots(); }
      return;
    }
    drag = null; brush?.remove(); brush = null;
    const h = pick(e);
    selectPeak(h ? { k, kind: h.kind, i: h.i } : null);
  });
  svg.addEventListener('pointerleave', () => { if (!drag) { tip.hidden = true; setHot(null); if (k === S.cand) previewFragment(null); } });
  svg.addEventListener('dblclick', () => { S.zoom = null; renderPlots(); });
}
function peakTip(k, sp, h) {
  if (h.kind === 'e') {
    const [mz, it] = sp.exp[h.i], pi = sp.e2p.get(h.i);
    const rel = `${(100 * it / sp.expMax).toFixed(1)}% of base peak`;
    if (pi == null) return `<b>Experimental m/z ${mz.toFixed(4)}</b><br>${rel}<br><span class="muted">Not explained by #${k + 1}</span>`;
    const f = S.formulaCache[`${k}:${sp.pair.atlas_key}:${pi}`];
    return `<b>Experimental m/z ${mz.toFixed(4)}</b><br>${rel}<br>Matched predicted ${sp.pred[pi][0].toFixed(4)} (${ppm(mz, sp.pred[pi][0]).toFixed(1)} ppm)${f ? `<br>${fhtml(f)}` : ''}`;
  }
  const [mz, it] = sp.pred[h.i], ei = sp.p2e.get(h.i), f = S.formulaCache[`${k}:${sp.pair.atlas_key}:${h.i}`];
  return `<b>Predicted m/z ${mz.toFixed(4)}</b>${f ? ` · ${fhtml(f)}` : ''}<br>${(100 * it / sp.predMax).toFixed(1)}% of predicted base peak<br>` +
    (ei != null ? `Matches experimental ${sp.exp[ei][0].toFixed(4)} (${ppm(sp.exp[ei][0], mz).toFixed(1)} ppm)` : '<span class="muted">No experimental match</span>') +
    (sp.ids[h.i] == null ? '<br><span class="muted">No fragment ID</span>' : '');
}
function placeTip(cx, cy) {
  const tip = $('tip'), r = tip.getBoundingClientRect();
  let x = cx + 14, y = cy + 14;
  if (x + r.width > innerWidth - 8) x = cx - r.width - 14;
  if (y + r.height > innerHeight - 8) y = cy - r.height - 14;
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
}
function bindTips(root) {
  const tip = $('tip');
  root.addEventListener('mousemove', e => {
    const t = e.target.closest('[data-tip]');
    if (!t) { tip.hidden = true; return; }
    tip.innerHTML = t.dataset.tip; tip.hidden = false; placeTip(e.clientX, e.clientY);
  });
  root.addEventListener('mouseleave', () => { tip.hidden = true; });
}

/* ---------- selection, structure highlighting, inspector ---------- */
function selectPeak(sel) {
  rememberFragmentDraft();
  if (sel && sel.k !== S.cand) {
    // Clicking the compared plot makes that candidate current.
    const sp = spectraFor(cand(sel.k));
    const pi = sel.kind === 'p' ? sel.i : sp?.e2p.get(sel.i);
    const prev = S.cand;
    S.cand = sel.k; S.compare = prev;
    S.peak = pi != null ? { k: S.cand, kind: 'p', i: pi } : { k: S.cand, kind: sel.kind, i: sel.i };
    renderRail(); renderEnergy(); renderPlots(); renderEvidence(); renderInspector(); renderVerdict();
    return;
  }
  if (sel && sel.kind === 'e') {
    const pi = spectraFor(cand()).e2p.get(sel.i);
    if (pi != null) sel = { k: sel.k, kind: 'p', i: pi };
  }
  S.peak = sel; renderPlots(); renderInspectorSelection();
}
function annotatedOrder() {
  const sp = spectraFor(cand());
  if (!sp) return [];
  return sp.pred.map((p, i) => i).filter(i => sp.ids[i] != null).sort((a, b) => sp.pred[a][0] - sp.pred[b][0]);
}
function stepPeak(dir) {
  const order = annotatedOrder(); if (!order.length) return;
  const cur = S.peak?.kind === 'p' ? order.indexOf(S.peak.i) : -1;
  const next = order[cur < 0 ? (dir > 0 ? 0 : order.length - 1) : (cur + dir + order.length) % order.length];
  const mz = spectraFor(cand()).pred[next][0];
  if (S.zoom && (mz < S.zoom[0] || mz > S.zoom[1])) S.zoom = null;
  selectPeak({ k: S.cand, kind: 'p', i: next });
}
function highlight(frag) {
  const box = $('struct'), svg = box.querySelector('svg');
  box.classList.toggle('dim', Boolean(frag));
  if (!svg) return;
  svg.querySelectorAll('.hl').forEach(el => el.classList.remove('hl'));
  if (!frag) return;
  frag.b.forEach(b => svg.querySelectorAll(`.bond-${b}`).forEach(el => el.classList.add('hl')));
  frag.a.forEach(a => svg.querySelectorAll(`.atom-${a}`).forEach(el => { if (!/\bbond-/.test(el.getAttribute('class'))) el.classList.add('hl'); }));
}
let currentPayload = null;
function fragmentFor(pl, sp, pi) {
  if (!pl || pi == null || !sp) return null;
  const id = sp.ids[pi];
  if (id == null) return null;
  return { id: String(id), formula: pl.peaks?.[sp.pair.atlas_key]?.[pi] || '', shape: pl.fragments?.[String(id)] || null };
}
function previewFragment(pi, sp) {
  if (!currentPayload) return;
  if (pi == null) { applySelectedHighlight(); return; }
  highlight(fragmentFor(currentPayload, sp || spectraFor(cand()), pi)?.shape || null);
}
function applySelectedHighlight() {
  const pi = S.peak?.kind === 'p' ? S.peak.i : null;
  highlight(pi != null ? fragmentFor(currentPayload, spectraFor(cand()), pi)?.shape || null : null);
}
function cacheFormulas(k, pl) {
  for (const [ce, rows] of Object.entries(pl.peaks || {})) rows.forEach((formula, i) => { if (formula) S.formulaCache[`${k}:${ce}:${i}`] = formula; });
}
async function renderInspector() {
  const k = S.cand, c = cand(), sp = spectraFor(c), r = S.r;
  currentPayload = null;
  $('struct').classList.remove('dim');
  $('struct').innerHTML = '<span class="sub">Drawing…</span>';
  $('cand-summary').innerHTML = `<div class="stat"><b>${fmt(sp?.pair.entropy_similarity ?? c.entropy_similarity, 3)}</b><span>entropy similarity</span></div>
    <div class="stat"><b>${pct(c.explained_intensity)}</b><span>intensity explained</span></div>
    <div class="stat"><b>${sp ? sp.e2p.size : 0}<span class="sub"> / ${S.result.peaks ?? (sp ? sp.exp.length : 0)}</span></b><span>peaks matched</span></div>
    <div class="smiles">${esc(c.smiles)}</div>`;
  renderPeakInfo(sp, null);
  $('frag-list').innerHTML = '';
  const pl = await payload(k);
  if (S.r !== r || S.cand !== k) return;
  currentPayload = pl; cacheFormulas(k, pl);
  if (S.compare != null) payload(S.compare).then(p => { cacheFormulas(S.compare, p); });
  $('struct').innerHTML = pl.svg || `<span class="sub">${esc(c.smiles)}</span>`;
  renderInspectorSelection();
  if (S.peak) renderPlots();
}
function renderInspectorSelection() {
  const sp = spectraFor(cand());
  applySelectedHighlight();
  renderPeakInfo(sp, currentPayload);
  renderFragList(sp, currentPayload);
}
function renderPeakInfo(sp, pl) {
  const note = $('frag-note'), box = $('peak-info');
  note.disabled = true; note.value = '';
  if (!sp || !S.peak) { box.innerHTML = '<span class="sub">Hover a peak to preview its fragment on the structure; click to pin it here.</span>'; return; }
  if (S.peak.kind === 'e') {
    const [mz, it] = sp.exp[S.peak.i];
    box.innerHTML = `<div>Experimental m/z <b>${mz.toFixed(4)}</b> · ${(100 * it / sp.expMax).toFixed(1)}%</div><div class="sub">Not explained by #${S.cand + 1}. Compare another candidate or try another formula.</div>`;
    return;
  }
  const i = S.peak.i, [mz, it] = sp.pred[i], ei = sp.p2e.get(i), f = fragmentFor(pl, sp, i);
  box.innerHTML = `${f?.formula ? `<div class="formula">${fhtml(f.formula)}</div>` : ''}
    <div>Predicted m/z <b>${mz.toFixed(4)}</b> · ${(100 * it / sp.predMax).toFixed(1)}%${f ? ` · fragment <span class="mono">${esc(f.id)}</span>` : ''}</div>
    <div>${ei != null ? `Experimental <b>${sp.exp[ei][0].toFixed(4)}</b> · ${(100 * sp.exp[ei][1] / sp.expMax).toFixed(1)}% · ${ppm(sp.exp[ei][0], mz).toFixed(1)} ppm` : '<span class="sub">No experimental peak within tolerance</span>'}</div>`;
  if (sp.ids[i] != null) {
    const key = `${sp.pair.atlas_key}:${i}`, draft = S.drafts.get(`${S.r}|${cand().review_key}|${key}`);
    note.disabled = READONLY();
    note.value = draft ?? noteFor(cand()).fragments?.[key]?.comment ?? '';
  }
}
function renderFragList(sp, pl) {
  if (!sp || !pl) { $('frag-list').innerHTML = ''; return; }
  const notes = noteFor(cand()).fragments || {};
  const seen = new Set(), rows = [];
  sp.pred.map((p, i) => i).filter(i => sp.ids[i] != null).sort((a, b) => sp.pred[b][1] - sp.pred[a][1]).forEach(i => {
    if (seen.has(String(sp.ids[i])) || rows.length >= 8) return; seen.add(String(sp.ids[i])); rows.push(i);
  });
  $('frag-list').innerHTML = rows.length ? rows.map(i => {
    const f = fragmentFor(pl, sp, i), key = `${sp.pair.atlas_key}:${i}`;
    return `<button class="frag-row" type="button" data-peak="${i}" aria-current="${S.peak?.kind === 'p' && S.peak.i === i}">
      <span>${f?.formula ? fhtml(f.formula) : '<span class="sub">—</span>'} <span class="sub">${sp.pred[i][0].toFixed(3)}</span>${notes[key] ? ' <span class="note-dot" title="Has a note">●</span>' : ''}</span>
      <span class="sub">${sp.p2e.has(i) ? 'matched' : 'unmatched'}</span>
      <span class="minibar"><i style="width:${100 * sp.pred[i][1] / sp.predMax}%"></i></span></button>`;
  }).join('') : '<span class="sub">No fragment IDs for this prediction.</span>';
}
function renderEvidence() {
  const c = cand(), sp = spectraFor(c), res = S.result;
  if (!sp) { $('evidence').innerHTML = ''; return; }
  const model = !String(c.source).startsWith('public');
  const unexplained = sp.exp.map((p, i) => i).filter(i => !sp.e2p.has(i) && sp.exp[i][0] <= sp.precursor + 2 && sp.exp[i][1] / sp.expMax >= 0.03)
    .sort((a, b) => sp.exp[b][1] - sp.exp[a][1]).slice(0, 10);
  const hyps = (res.formula_hypotheses || []).map(h => h.formula).filter(f => f && f !== res.formula);
  const pair = sp.pair;
  $('evidence').innerHTML = `<div class="ev-grid">
    <div class="ev-item"><h3>Match</h3>
      <p>Entropy similarity <b>${fmt(pair.entropy_similarity ?? c.entropy_similarity, 3)}</b> · explained intensity <b>${pct(c.explained_intensity)}</b></p>
      <p>Experimental <b>${fmt(pair.input_value, 0)} ${esc(pair.input_unit)}</b> paired with the prediction at <b>${fmt(pair.atlas_ev, 0)} eV</b>${pair.delta_ev != null ? ` (Δ ${fmt(pair.delta_ev, 1)} eV)` : ''}</p>
      <p class="sub">Similarity ranks candidates for this spectrum; it is not identification confidence.</p></div>
    <div class="ev-item"><h3>Provenance</h3>
      <p>${model ? `<b>${esc(modelLabel(c))} prediction</b> at ${esc((c.model_collision_energies_ev || []).join(', '))} eV${c.model_instrument ? ` as ${esc(c.model_instrument)}` : ''}${res.review?.proposal_source === 'pubchem' ? ' for PubChem structures' : res.review?.proposal_source === 'atlas' ? ' for ICEBERG Atlas structures' : ''}` : `<b>ICEBERG Atlas</b> (precomputed ${esc(String(c.source).match(/ICEBERG [\d.]+/)?.[0] || 'ICEBERG')} prediction for a PubChem structure)`}</p>
      ${model && c.model_checkpoint ? `<p class="sub" title="${esc([].concat(c.model_checkpoint).join('\n'))}">Weights: ${esc(weightsLabel(c.model_checkpoint))}${c.ms_pred_version ? ` · ms-pred ${esc(c.ms_pred_version)}` : ''}</p>` : ''}
      <p>Formula <b>${fhtml(res.formula)}</b> <span class="sub">${esc(res.formula_source || '')}</span></p>
      ${hyps.length ? `<p>Other formula hypotheses:</p>${hypList(hyps)}` : ''}</div>
    <div class="ev-item"><h3>Strongest unexplained peaks</h3>
      ${unexplained.length ? `<div class="chips">${unexplained.map(i => `<button class="chip" type="button" data-unexplained="${i}">${sp.exp[i][0].toFixed(3)} · ${(100 * sp.exp[i][1] / sp.expMax).toFixed(0)}%</button>`).join('')}</div>` : '<p class="sub">Every peak above 3% is explained by this candidate.</p>'}</div>
  </div>${(res.warnings || []).filter(w => !(res.candidates.length && String(w).startsWith('No comparable atlas'))).map(w => `<p class="warn">⚠ ${esc(String(w).split('\n')[0].slice(0, 240))}</p>`).join('')}`;
}

/* ---------- verdict and autosave ---------- */
function currentReview() {
  const c = cand(), draft = S.drafts.get(`${S.r}|${c.review_key}`) || {}, note = noteFor(c);
  return { decision: draft.decision || note.decision || 'unreviewed', comment: draft.comment ?? note.comment ?? '' };
}
function renderVerdict() {
  const rv = currentReview(), note = noteFor(cand());
  $('v-rank').textContent = `#${S.cand + 1}`;
  document.querySelectorAll('#decision button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.d === rv.decision)));
  $('cand-comment').value = rv.comment;
  $('cand-comment').disabled = READONLY();
  $('workspace').classList.toggle('readonly', READONLY());
  $('demo-chip').hidden = !DEMO();
  $('save-state').textContent = READONLY() ? 'Read-only snapshot'
    : DEMO() ? 'Demo · saved in this browser only'
    : (note.updated_at ? `Saved ${new Date(note.updated_at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}` : 'Autosaves as you review');
}
function setDraft(patch) { S.drafts.set(`${S.r}|${cand().review_key}`, { ...currentReview(), ...patch }); }
function rememberFragmentDraft() {
  if (!S.result?.candidates.length || S.peak?.kind !== 'p' || $('frag-note').disabled) return;
  const sp = spectraFor(cand());
  S.drafts.set(`${S.r}|${cand().review_key}|${sp.pair.atlas_key}:${S.peak.i}`, $('frag-note').value);
}
function scheduleSave(kind, delay) {
  if (READONLY()) return;
  const r = S.r, k = S.cand, c = cand(), sp = spectraFor(c), peak = S.peak;
  const fragKey = kind === 'fragment' && peak?.kind === 'p' && sp ? `${sp.pair.atlas_key}:${peak.i}` : null;
  const timerKey = `${r}|${c.review_key}|${fragKey || kind}`;
  clearTimeout(S.saveTimers[timerKey]);
  $('save-state').textContent = 'Editing…';
  S.saveTimers[timerKey] = setTimeout(async () => {
    const rv = S.drafts.get(`${r}|${c.review_key}`) || {};
    const note = S.r === r ? noteFor(c) : {};
    const body = { result: r, candidate_key: c.review_key, decision: rv.decision || note.decision || 'unreviewed', comment: rv.comment ?? note.comment ?? '' };
    if (fragKey) body.fragment = { ce: sp.pair.atlas_key, peak_index: peak.i, comment: S.drafts.get(`${r}|${c.review_key}|${fragKey}`) ?? '' };
    if (S.r === r) $('save-state').textContent = 'Saving…';
    try {
      const data = await api.save(body);
      S.index[r].review = data.review; S.index[r].reviewed = data.reviewed;
      if (S.r === r) {
        S.notes = data.notes;
        $('save-state').textContent = `${DEMO() ? 'Demo · saved in this browser' : 'Saved'} ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
        renderRail();
        if (S.cand === k && fragKey) renderFragList(spectraFor(cand()), currentPayload);
      }
    } catch (error) { if (S.r === r) $('save-state').textContent = `Not saved: ${error.message}`; }
  }, delay);
}
function decide(d) {
  if (READONLY() || !S.result?.candidates.length) return;
  setDraft({ decision: d });
  renderVerdict(); renderRail();
  scheduleSave('candidate', 0);
}

/* ---------- navigation, palette, keyboard ---------- */
function route() {
  // Links use plain tokens (#r12, #overview) so they survive hosts that only pass simple anchors.
  const h = location.hash, legacy = h.match(/result=(\d+)/), m = h.match(/^#(?:\/r\/|r)(\d+)$/);
  const i = m ? Number(m[1]) : legacy ? Number(legacy[1]) : null;
  if (i != null && S.index[i]) {
    $('overview').hidden = true; $('workspace').hidden = false;
    openResult(i);
  } else {
    $('workspace').hidden = true; $('overview').hidden = false;
    renderOverview();
  }
}
function go(i) { if (i != null && S.index[i]) location.hash = `#r${i}`; }
function stepUnknown(dir) {
  if (!S.order.length) return;
  const pos = S.order.indexOf(S.r);
  go(S.order[pos < 0 ? 0 : (pos + dir + S.order.length) % S.order.length]);
}
function selectCandidate(k) {
  if (!S.result || k < 0 || k >= S.result.candidates.length) return;
  rememberFragmentDraft();
  S.cand = k; S.peak = null; S.pairIdx = 0;
  if (S.compare === k) S.compare = null;
  renderRail(); renderEnergy(); renderPlots(); renderEvidence(); renderInspector(); renderVerdict();
  document.querySelector(`.cand[data-k="${k}"]`)?.scrollIntoView({ block: 'nearest' });
  for (let j = k + 1; j < Math.min(S.result.candidates.length, k + 4); j++) payload(j).then(p => fillMini(j, p));
}
function toggleCompare() {
  if (!S.result || S.result.candidates.length < 2) return;
  S.compare = S.compare == null ? (S.cand === 0 ? 1 : 0) : null;
  if (S.compare != null) payload(S.compare).then(p => { cacheFormulas(S.compare, p); });
  renderEnergy(); renderRail(); renderPlots();
}
let palSel = 0;
function openPalette() {
  const d = $('palette'); if (d.open) return;
  $('pal-q').value = ''; palSel = 0; renderPalette(); d.showModal(); $('pal-q').focus();
}
function palRows() {
  const q = $('pal-q').value.trim().toLowerCase();
  return S.index.filter(r => !q || [r.label, r.formula, fmt(r.parentmass, 4), r.top_smiles].join(' ').toLowerCase().includes(q)).slice(0, 40);
}
function renderPalette() {
  const rows = palRows(); palSel = Math.min(palSel, Math.max(0, rows.length - 1));
  $('pal-list').innerHTML = rows.map((r, n) => `<li data-i="${r.index}" aria-selected="${n === palSel}"><span><b>${esc(r.label)}</b> <small>${r.formula ? fhtml(r.formula) : 'no formula'}</small></span><small>m/z ${fmt(r.parentmass, 4)}</small><small>${r.candidates ? fmt(r.top_similarity, 3) : '—'}</small></li>`).join('') || '<li class="sub">No matches</li>';
  $('pal-list').querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
}
function copyFormulaCommand(f) {
  const res = S.result, input = res?.input || 'sample.ms', unit = res?.collision_unit || 'eV';
  const out = `results/${S.index[S.r]?.label || 'sample'}_alt_${f}`;
  const cmd = `msms-structure-elucidation run --input ${input} --collision-unit ${unit} --formula ${f} --output-dir ${out}`;
  const done = () => toast(`Copied: <span class="mono">${esc(cmd)}</span>`);
  if (navigator.clipboard) navigator.clipboard.writeText(cmd).then(done, () => toast(`<span class="mono">${esc(cmd)}</span>`, 12000));
  else toast(`<span class="mono">${esc(cmd)}</span>`, 12000);
}
let toastTimer = null;
function toast(html, ms = 5000) { const t = $('toast'); t.innerHTML = html; t.hidden = false; clearTimeout(toastTimer); if (ms) toastTimer = setTimeout(() => { t.hidden = true; }, ms); }
function showError(msg) { const e = $('error'); e.textContent = msg; e.hidden = !msg; }

async function exportReport() {
  if (STATIC) return;
  try {
    let st = await getJSON('/api/export', { method: 'POST', headers: { 'X-Review-Token': S.token || '' } });
    toast(`Exporting report… 0/${st.total}`, 0);
    while (st.state === 'running') {
      await new Promise(r => setTimeout(r, 800));
      st = await getJSON('/api/export');
      toast(`Exporting report… ${st.done}/${st.total}`, 0);
    }
    if (st.state === 'done') toast(`Report written to <span class="mono">${esc(st.path)}</span> · <a href="/export/review_report.html">Download</a>`, 20000);
    else toast(`Export failed: ${esc(st.error || 'unknown error')}`, 10000);
  } catch (error) { toast(`Export failed: ${esc(error.message)}`, 10000); }
}

function onKey(e) {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === 'Escape') {
    if (document.querySelector('dialog[open]')) return;
    if (typing) { e.target.blur(); return; }
    if (!$('workspace').hidden) { if (S.peak) selectPeak(null); else location.hash = '#overview'; }
    return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return;
  if (e.key === '/') { e.preventDefault(); openPalette(); return; }
  if (e.key === '?') { $('shortcuts').showModal(); return; }
  if ($('workspace').hidden) return;
  const has = S.result?.candidates.length > 0;
  const act = {
    j: () => stepUnknown(1), k: () => stepUnknown(-1),
    ArrowDown: () => has && selectCandidate(S.cand + 1), ArrowUp: () => has && selectCandidate(S.cand - 1),
    ArrowRight: () => has && stepPeak(1), ArrowLeft: () => has && stepPeak(-1),
    1: () => decide('keep'), 2: () => decide('uncertain'), 3: () => decide('reject'), 0: () => decide('unreviewed'),
    c: () => toggleCompare(), z: () => { if (has) { S.zoom = null; renderPlots(); } },
  }[e.key];
  if (act) { e.preventDefault(); act(); }
}
function wire() {
  ['f-q', 'f-outcome', 'f-review', 'f-sim'].forEach(id => $(id).addEventListener('input', () => { $('f-sim-v').textContent = Number($('f-sim').value).toFixed(2); renderTable(); }));
  document.querySelectorAll('#ov-table th[data-k]').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.k;
    S.sort = S.sort.key === k ? { key: k, dir: -S.sort.dir } : { key: k, dir: ['top_similarity', 'top_explained'].includes(k) ? -1 : 1 };
    renderTable();
  }));
  const openRow = e => { const tr = e.target.closest('tr[data-i]'); if (tr) go(Number(tr.dataset.i)); };
  $('ov-table').tBodies[0].addEventListener('click', openRow);
  $('ov-table').tBodies[0].addEventListener('keydown', e => { if (e.key === 'Enter') openRow(e); });
  bindTips($('hist'));
  $('cands').addEventListener('click', e => { const b = e.target.closest('.cand'); if (b) selectCandidate(Number(b.dataset.k)); });
  $('energy').addEventListener('change', () => { S.pairIdx = Number($('energy').value); S.peak = null; S.zoom = null; renderPlots(); renderEvidence(); renderInspector(); });
  $('compare').addEventListener('click', toggleCompare);
  $('compare-with').addEventListener('change', () => { S.compare = Number($('compare-with').value); payload(S.compare).then(p => cacheFormulas(S.compare, p)); renderRail(); renderPlots(); });
  $('reset-zoom').addEventListener('click', () => { S.zoom = null; renderPlots(); });
  $('frag-list').addEventListener('click', e => {
    const b = e.target.closest('[data-peak]'); if (!b) return;
    const i = Number(b.dataset.peak), mz = spectraFor(cand()).pred[i][0];
    if (S.zoom && (mz < S.zoom[0] || mz > S.zoom[1])) S.zoom = null;
    selectPeak({ k: S.cand, kind: 'p', i });
  });
  $('frag-list').addEventListener('mouseover', e => { const b = e.target.closest('[data-peak]'); if (b) previewFragment(Number(b.dataset.peak)); });
  $('frag-list').addEventListener('mouseleave', () => previewFragment(null));
  $('evidence').addEventListener('click', e => {
    const u = e.target.closest('[data-unexplained]'); if (!u) return;
    const i = Number(u.dataset.unexplained), mz = spectraFor(cand()).exp[i][0];
    S.zoom = [Math.max(0, mz - 25), mz + 25]; selectPeak({ k: S.cand, kind: 'e', i });
  });
  document.addEventListener('click', e => { const b = e.target.closest('[data-copy-formula]'); if (b) copyFormulaCommand(b.dataset.copyFormula); });
  $('decision').addEventListener('click', e => { const b = e.target.closest('[data-d]'); if (b) decide(b.dataset.d); });
  $('cand-comment').addEventListener('input', () => { setDraft({ comment: $('cand-comment').value }); scheduleSave('candidate', 700); });
  $('frag-note').addEventListener('input', () => { rememberFragmentDraft(); scheduleSave('fragment', 700); });
  $('prev').addEventListener('click', () => stepUnknown(-1));
  $('next').addEventListener('click', () => stepUnknown(1));
  $('open-search').addEventListener('click', openPalette);
  $('help').addEventListener('click', () => $('shortcuts').showModal());
  $('export').addEventListener('click', exportReport);
  $('theme').addEventListener('click', () => {
    const dark = getComputedStyle(document.documentElement).colorScheme.includes('dark');
    document.documentElement.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('msms-theme', document.documentElement.dataset.theme); } catch (e) {}
  });
  $('pal-q').addEventListener('input', () => { palSel = 0; renderPalette(); });
  $('pal-q').addEventListener('keydown', e => {
    const rows = palRows();
    if (e.key === 'ArrowDown') { palSel = Math.min(rows.length - 1, palSel + 1); renderPalette(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { palSel = Math.max(0, palSel - 1); renderPalette(); e.preventDefault(); }
    else if (e.key === 'Enter' && rows[palSel]) { $('palette').close(); go(rows[palSel].index); }
  });
  $('pal-list').addEventListener('click', e => { const li = e.target.closest('li[data-i]'); if (li) { $('palette').close(); go(Number(li.dataset.i)); } });
  $('palette').addEventListener('click', e => { if (e.target === $('palette')) $('palette').close(); });
  document.addEventListener('keydown', onKey);
  window.addEventListener('hashchange', route);
  let rt = null;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => { if (!$('workspace').hidden && S.result?.candidates.length) renderPlots(); }, 120); });
}

async function init() {
  wire();
  try {
    if (window.MSMS_STATIC_SRC && !STATIC) $('dataset').textContent = 'Loading spectra…';
    await loadStatic();
    const data = await api.index();
    S.index = data.results;
    if (data.review_token) S.token = data.review_token;
    const first = S.index[0]?.path || '';
    const folder = first.split('/').slice(-3, -2)[0] || '';
    $('dataset').textContent = `${S.index.length} unknown${S.index.length === 1 ? '' : 's'}${folder ? ` · ${folder}/` : ''}`;
    if (STATIC) {
      $('export').hidden = true;
      $('dataset').textContent += ` · snapshot ${new Date(STATIC.exported_at).toLocaleDateString()} · top ${STATIC.top_k} candidates each`;
      $('dataset').title = `Exported ${new Date(STATIC.exported_at).toLocaleString()}.`;
    }
    if (DEMO()) {
      $('demo-note').hidden = false;
      const store = demoStore();  // reflect this browser's demo reviews in the overview
      for (const [i, notes] of Object.entries(store)) {
        if (!S.index[i]) continue;
        S.index[i].review = reviewSummary(notes, STATIC.results[i].result.candidates.map(c => c.review_key));
        S.index[i].reviewed = S.index[i].review.reviewed;
      }
    }
    S.order = filtered().map(r => r.index);
    if (S.index.length === 1 && !location.hash) location.hash = '#r0';
    route();
  } catch (error) { showError(`Cannot load results: ${error.message}`); }
}
init();
