/* Copyright (c) 2026 Coley Group. */
const $ = id => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
const state = { result: null, notes: null, token: null, candidate: 0, pair: null,
  peak: null, minMz: 0, maxMz: 1, generation: 0, fragmentCache: new Map(), drafts: new Map() };

function svgElement(tag, attrs = {}) {
  const el = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  return el;
}
function candidate() { return state.result.candidates[state.candidate]; }
function pair() { return state.pair; }
function noteForCurrent() { return state.notes.candidates[candidate().review_key] || {}; }
function fragmentNoteKey() { return state.peak ? `${state.peak.ce}:${state.peak.index}` : null; }
function rememberDraft() {
  if (!state.result) return;
  const key = candidate().review_key;
  state.drafts.set(key, { decision: $('decision').value, comment: $('candidate-comment').value });
  if (state.peak) state.drafts.set(`${key}|${fragmentNoteKey()}`, $('fragment-comment').value);
}
function restoreReview() {
  const note = noteForCurrent(), draft = state.drafts.get(candidate().review_key);
  $('decision').value = draft?.decision || note.decision || 'unreviewed';
  $('candidate-comment').value = draft?.comment ?? note.comment ?? '';
  const fragmentKey = fragmentNoteKey();
  $('fragment-comment').disabled = !fragmentKey;
  $('fragment-comment').value = fragmentKey
    ? (state.drafts.get(`${candidate().review_key}|${fragmentKey}`) ?? note.fragments?.[fragmentKey]?.comment ?? '') : '';
  $('save-status').textContent = '';
}
function makeCandidateButton(c, index) {
  const button = document.createElement('button');
  button.className = `candidate ${index === state.candidate ? 'active' : ''}`;
  button.type = 'button';
  const rank = document.createElement('strong'); rank.textContent = `#${index + 1}`;
  const img = document.createElement('img'); img.alt = ''; img.src = c.structure_image || '';
  const text = document.createElement('span');
  const score = document.createElement('strong'); score.textContent = Number(c.entropy_similarity).toFixed(4);
  const small = document.createElement('small'); small.textContent = c.smiles;
  text.append(score, small); button.append(rank, img, text);
  button.onclick = () => { rememberDraft(); state.candidate = index; selectCandidate(); };
  return button;
}
function selectCandidate() {
  state.generation++;
  $('candidate-list').replaceChildren(...state.result.candidates.map(makeCandidateButton));
  const c = candidate();
  $('structure').src = c.structure_image || '';
  $('candidate-title').textContent = `Rank ${state.candidate + 1} · ${c.source}`;
  $('candidate-meta').textContent = `${c.formula || state.result.formula || ''} · ${c.inchikey || 'No InChIKey'}`;
  $('smiles').textContent = c.smiles;
  $('metrics').replaceChildren();
  for (const [label, value] of [['Entropy similarity', Number(c.entropy_similarity).toFixed(4)],
                                ['Explained intensity', `${(100 * Number(c.explained_intensity)).toFixed(1)}%`],
                                ['Matched peaks', String(c.matched_peaks?.length || 0)]]) {
    const item = document.createElement('div'); item.className = 'metric';
    const valueEl = document.createElement('strong'); valueEl.textContent = value;
    const labelEl = document.createElement('span'); labelEl.textContent = label;
    item.append(valueEl, labelEl); $('metrics').append(item);
  }
  const alignments = c.energy_alignment || [];
  const selector = $('energy'); selector.replaceChildren();
  for (const alignment of alignments) {
    const option = document.createElement('option'); option.value = alignment.experimental_key;
    option.textContent = `${alignment.input_value} ${alignment.input_unit} → ${Number(alignment.experimental_ev).toFixed(2)} eV; prediction ${alignment.atlas_ev} eV`;
    selector.append(option);
  }
  selector.disabled = !alignments.length;
  state.pair = alignments[0] || null; state.peak = null;
  resetZoom(); restoreReview(); renderEnergy();
}
function getPeaks() {
  if (!pair()) return { experimental: [], predicted: [], ids: [] };
  const c = candidate();
  return { experimental: state.result.spectra[pair().experimental_key] || [],
    predicted: c.predicted_spectra?.[pair().atlas_key] || [],
    ids: c.predicted_fragment_ids?.[pair().atlas_key] || [] };
}
function globalMaxMz() {
  const c = candidate();
  return Math.max(Number(state.result.parentmass) || 1,
    ...Object.values(state.result.spectra).flat().map(p => Number(p[0])),
    ...Object.values(c.predicted_spectra || {}).flat().map(p => Number(p[0]))) * 1.03;
}
function resetZoom() {
  state.minMz = 0; state.maxMz = globalMaxMz();
  $('mz-min').value = '0'; $('mz-max').value = state.maxMz.toFixed(1);
}
function renderEnergy() {
  state.generation++;
  const { experimental, predicted, ids } = getPeaks();
  $('fragment-detail').textContent = 'Click an annotated predicted peak or a matched experimental peak.';
  $('fragment-strip').replaceChildren();
  if (!pair()) {
    $('energy-note').textContent = 'No experimental energy was paired with a predicted spectrum.';
    $('annotation-note').textContent = '';
    $('mirror').replaceChildren(); return;
  }
  $('energy-note').textContent = `${experimental.length} experimental peaks at ${pair().input_value} ${pair().input_unit} (${Number(pair().experimental_ev).toFixed(2)} eV) compared with ${predicted.length} predicted peaks at ${pair().atlas_ev} eV. Entropy similarity ${Number(pair().entropy_similarity || 0).toFixed(3)}.`;
  $('annotation-note').textContent = ids.length === predicted.length && ids.length
    ? `${ids.length} predicted peaks have fragment IDs. Click a peak or thumbnail to highlight its atoms and bonds.`
    : 'Fragment IDs are unavailable for this spectrum; the mirror plot remains available.';
  drawMirror(); renderFragmentStrip();
}
function drawMirror() {
  const svg = $('mirror'); svg.replaceChildren();
  if (!pair()) return;
  const { experimental, predicted, ids } = getPeaks();
  const min = state.minMz, width = state.maxMz - min;
  if (!(width > 0)) return;
  const x = mz => 42 + (Number(mz) - min) / width * 920;
  const y0 = 211;
  const maxExp = Math.max(1, ...experimental.map(p => Number(p[1])));
  const maxPred = Math.max(1, ...predicted.map(p => Number(p[1])));
  svg.append(svgElement('line', { x1: 42, y1: y0, x2: 962, y2: y0, class: 'axis' }));
  for (let t = 0; t <= 5; t++) {
    const mz = min + width * t / 5, xpos = x(mz);
    svg.append(svgElement('line', { x1: xpos, y1: y0 - 4, x2: xpos, y2: y0 + 4, class: 'axis' }));
    const label = svgElement('text', { x: xpos, y: 422, 'text-anchor': 'middle', fill: '#586a7a', 'font-size': 12 });
    label.textContent = mz.toFixed(1); svg.append(label);
  }
  const matched = (candidate().matched_peaks || []).filter(p => String(p.ce) === pair().experimental_key);
  predicted.forEach(([mz, intensity], index) => {
    if (mz < min || mz > state.maxMz) return;
    const annotated = ids[index] != null;
    const line = svgElement('line', { x1: x(mz), x2: x(mz), y1: y0, y2: y0 - 180 * Number(intensity) / maxPred,
      class: `pred-peak${annotated ? ' annotated' : ''}${state.peak?.index === index ? ' selected' : ''}`, 'data-peak': index });
    const title = svgElement('title'); title.textContent = `Predicted m/z ${Number(mz).toFixed(4)}, intensity ${Number(intensity).toFixed(4)}${annotated ? ', fragment ' + ids[index] : ''}`;
    line.append(title);
    line.onclick = () => selectPeak(index);
    line.onmouseenter = () => focusPeak(index, true);
    line.onmouseleave = () => focusPeak(index, false);
    svg.append(line);
  });
  experimental.forEach(([mz, intensity]) => {
    if (mz < min || mz > state.maxMz) return;
    const match = matched.find(p => Math.abs(Number(p.mz) - Number(mz)) < 1e-5);
    const line = svgElement('line', { x1: x(mz), x2: x(mz), y1: y0, y2: y0 + 180 * Number(intensity) / maxExp,
      class: `exp-peak${match ? ' matched' : ''}` });
    const title = svgElement('title'); title.textContent = `Experimental m/z ${Number(mz).toFixed(4)}, intensity ${Number(intensity).toFixed(2)}`;
    line.append(title);
    line.onclick = () => {
      if (!match) { state.peak = null; $('fragment-detail').textContent = 'Experimental peak not explained by the selected prediction.'; restoreReview(); drawMirror(); return; }
      const index = predicted.findIndex(p => Math.abs(Number(p[0]) - Number(match.predicted_mz)) < 1e-5);
      if (index >= 0) selectPeak(index);
    };
    svg.append(line);
  });
}
function focusPeak(index, active) {
  document.querySelectorAll(`[data-peak="${index}"]`).forEach(el => el.classList.toggle('focused', active));
}
async function fragmentData(index) {
  const key = `${state.candidate}|${pair().atlas_key}|${index}`;
  if (state.fragmentCache.has(key)) return state.fragmentCache.get(key);
  const response = await fetch(`/api/fragment/${state.candidate}/${encodeURIComponent(pair().atlas_key)}/${index}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Fragment request failed (${response.status})`);
  state.fragmentCache.set(key, data);
  return data;
}
function svgImage(svg) {
  const img = document.createElement('img');
  img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  img.alt = 'Predicted fragment highlighted on candidate structure';
  return img;
}
async function selectPeak(index) {
  rememberDraft();
  const { predicted, ids } = getPeaks();
  state.peak = ids[index] != null ? { ce: pair().atlas_key, index } : null;
  restoreReview(); drawMirror();
  document.querySelectorAll('.fragment-card').forEach(el => el.classList.toggle('active', Number(el.dataset.peak) === index));
  const detail = $('fragment-detail');
  if (ids[index] == null) { detail.textContent = 'No fragment assignment is available for this predicted peak.'; return; }
  detail.textContent = 'Loading fragment…';
  const generation = state.generation, currentCandidate = state.candidate;
  try {
    const data = await fragmentData(index);
    if (generation !== state.generation || currentCandidate !== state.candidate || state.peak?.index !== index) return;
    const meta = document.createElement('p');
    meta.textContent = `Fragment ${data.fragment_id} · predicted m/z ${Number(data.mz).toFixed(4)} · intensity ${Number(data.intensity).toFixed(4)}${data.formula ? ' · formula ' + data.formula : ''}`;
    detail.replaceChildren(svgImage(data.svg), meta);
  } catch (error) { if (generation === state.generation) detail.textContent = error.message; }
}
async function renderFragmentStrip() {
  const { predicted, ids } = getPeaks();
  const count = Number($('top-fragments').value);
  $('top-count').textContent = String(count);
  if (count === 0) { $('fragment-strip').replaceChildren(); return; }
  const ranked = predicted.map((p, index) => ({ index, intensity: Number(p[1]), fragment: ids[index] }))
    .filter(p => p.fragment != null).sort((a, b) => b.intensity - a.intensity);
  const distinct = [], seen = new Set();
  for (const item of ranked) {
    if (seen.has(String(item.fragment))) continue;
    seen.add(String(item.fragment)); distinct.push(item);
    if (distinct.length >= count) break;
  }
  const generation = state.generation, holder = $('fragment-strip');
  holder.replaceChildren();
  for (const item of distinct) {
    const card = document.createElement('button'); card.className = 'fragment-card';
    card.type = 'button'; card.dataset.peak = String(item.index);
    const label = document.createElement('small'); label.textContent = `m/z ${Number(predicted[item.index][0]).toFixed(3)}`;
    card.append(label); card.onclick = () => selectPeak(item.index);
    card.onmouseenter = () => focusPeak(item.index, true);
    card.onmouseleave = () => focusPeak(item.index, false);
    holder.append(card);
    fragmentData(item.index).then(data => {
      if (generation !== state.generation) return;
      card.prepend(svgImage(data.svg));
    }).catch(() => { if (generation === state.generation) card.title = 'Fragment image unavailable'; });
  }
}
async function saveReview() {
  const c = candidate();
  const body = { candidate_key: c.review_key, decision: $('decision').value,
    comment: $('candidate-comment').value };
  if (state.peak && $('fragment-comment').value != null) {
    body.fragment = { ce: state.peak.ce, peak_index: state.peak.index,
      comment: $('fragment-comment').value };
  }
  $('save-status').textContent = 'Saving…';
  try {
    const response = await fetch('/api/review', { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-Review-Token': state.token }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Save failed (${response.status})`);
    state.notes = data.notes; state.drafts.delete(c.review_key);
    if (state.peak) state.drafts.delete(`${c.review_key}|${fragmentNoteKey()}`);
    $('save-status').textContent = 'Saved to review_notes.json';
  } catch (error) { $('save-status').textContent = error.message; }
}

$('energy').onchange = () => {
  rememberDraft(); state.pair = candidate().energy_alignment.find(p => p.experimental_key === $('energy').value);
  state.peak = null; restoreReview(); renderEnergy();
};
$('zoom-apply').onclick = () => {
  const min = Number($('mz-min').value), max = Number($('mz-max').value);
  if (!Number.isFinite(min) || !Number.isFinite(max) || min < 0 || max <= min) {
    $('error').textContent = 'Enter a valid m/z range.'; return;
  }
  $('error').textContent = ''; state.minMz = min; state.maxMz = max; drawMirror();
};
$('zoom-reset').onclick = () => { resetZoom(); drawMirror(); };
$('top-fragments').oninput = () => renderFragmentStrip();
$('save-review').onclick = saveReview;

fetch('/api/state').then(response => response.json()).then(data => {
  state.result = data.result; state.notes = data.notes; state.token = data.review_token;
  $('summary').textContent = `${data.result.input?.split('/').pop() || 'Experimental spectrum'} · ${data.result.candidates.length} candidates · ${data.result.formula || 'unknown formula'} · experimental unit ${data.result.collision_unit || 'unspecified'}`;
  $('source-warning').textContent = (data.result.warnings || []).join(' ');
  if (data.result.candidates.length) selectCandidate();
  else $('detail').textContent = 'No ranked candidates are available in this result.';
}).catch(error => { $('error').textContent = `Cannot load result: ${error.message}`; });
