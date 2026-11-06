/* Exercise pagination and experimental/predicted spectrum views without a browser. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');

const root = path.resolve(__dirname, '..');
const { mergePeaks, spectrumQC } = require(path.join(root, 'src/msms_structure_elucidation/visualize_assets/viewer_merge.js'));
const grouped = mergePeaks([
  { key: '10', peaks: [[100, 1], [150, 0.5]], ids: ['A', 'C'] },
  { key: '20', peaks: [[100.001, 2], [200, 1]], ids: ['B', 'D'] },
]);
assert.equal(grouped.peaks.length, 3);
assert.ok(Math.abs(grouped.peaks[0][0] - (100 + 2 * 100.001) / 3) < 1e-9);
assert.equal(grouped.peaks[0][1], 3);
assert.deepEqual(grouped.members[0].map(p => [p.key, p.index, p.id]), [['10', 0, 'A'], ['20', 0, 'B']]);
assert.equal(mergePeaks([{ key: 'x', peaks: [[100, 1], [100.003, 2], [101, 0]] }]).peaks.length, 2);

const elements = Object.fromEntries(['energy', 'compare', 'compare-with', 'legend'].map(id => [id, {
  innerHTML: '', value: '', disabled: false, hidden: false, setAttribute() {},
}]));
const context = vm.createContext({
  window: { MSMSMerge: { mergePeaks } },
  document: { getElementById: id => elements[id] },
});
const source = fs.readFileSync(path.join(root, 'src/msms_structure_elucidation/visualize_assets/viewer.js'), 'utf8');
const withoutInit = source.replace(/\ninit\(\);\s*$/, '\n');
assert.notEqual(withoutInit, source, 'the test must disable only the browser bootstrap');
vm.runInContext(withoutInit, context);
elements.hist = { innerHTML: '' };
context.histogramScores = [-2e-9, 0.5, 1.00000001, null, NaN, Infinity, '0.7'];
vm.runInContext('renderHist(histogramScores)', context);
assert.match(elements.hist.innerHTML, /Similarity 0\.00–0\.05<br><b>1<\/b> unknowns/);
assert.match(elements.hist.innerHTML, /Similarity 0\.95–1\.00<br><b>1<\/b> unknowns/);
assert.doesNotMatch(elements.hist.innerHTML, /NaN|Infinity/);
assert.equal(context.histogramScores[0], -2e-9, 'Histogram bins must not alter scientific scores');
for (const size of [0, 1, 5, 100, 3500, 10000]) {
  context.histogramScores = Array(size).fill(0.5);
  vm.runInContext('renderHist(histogramScores)', context);
  const ticks = [...elements.hist.innerHTML.matchAll(/<g class="axis y-axis">.*?<text[^>]*>([^<]+)<\/text><\/g>/g)]
    .map(match => Number(match[1].replace(/,/g, '')));
  assert.ok(ticks.length >= 2 && ticks.length <= 6, `Readable tick count for ${size} features`);
  assert.equal(ticks[0], 0);
  assert.ok(ticks.at(-1) >= size, 'The count axis must cover the largest histogram bin');
  assert.ok(ticks.every(Number.isInteger), 'Count labels must stay integral');
}
const result = {
  parentmass: 250,
  spectra: { '10.0': [[100, 1]], '20.0': [[100.001, 2]] },
  candidates: [{
    smiles: 'CCO', source: 'public ICEBERG 2.1 PubChem atlas', entropy_similarity: 0.7,
    predicted_spectra: { '10': [[100.0002, 1]], '20': [[100.0006, 2]] },
    predicted_fragment_ids: { '10': ['A'], '20': ['B'] },
    energy_alignment: [
      { experimental_key: '10.0', atlas_key: '10', input_value: 10, input_unit: 'NCE', model_energy_ev: 10 },
      { experimental_key: '20.0', atlas_key: '20', input_value: 20, input_unit: 'NCE', model_energy_ev: 20 },
    ],
    matched_peaks: [
      { ce: '10.0', mz: 100, predicted_mz: 100.0002 },
      { ce: '20.0', mz: 100.001, predicted_mz: 100.0006 },
    ],
  }],
};
context.fixture = result;
vm.runInContext('S.result = fixture; S.cand = 0; S.pairIdx = -1;', context);
const sp = vm.runInContext('spectraFor(cand())', context);
assert.equal(sp.merged, true);
assert.equal(sp.exp.length, 1);
assert.equal(sp.pred.length, 1);
assert.equal(sp.e2p.get(0), 0);
assert.equal(sp.p2e.get(0), 0);
assert.equal(sp.ids[0], 'B', 'strongest annotated contributor supplies the visible fragment');
assert.equal(vm.runInContext('peakNoteKey(spectraFor(cand()), 0)', context), '20:0');
assert.equal(vm.runInContext("fragmentFor({peaks: {'20': ['C2H4']}, fragments: {B: {a: [0], b: []}}}, spectraFor(cand()), 0).source", context), '20');
vm.runInContext('renderEnergy()', context);
assert.match(elements.energy.innerHTML, /value="-1">Merged all energies/);
assert.equal(elements.energy.value, '-1');
vm.runInContext('S.pairIdx = 0;', context);
assert.equal(vm.runInContext('spectraFor(cand()).merged', context), false);
assert.equal(vm.runInContext('spectraFor(cand()).key', context), '10');

elements.evidence = { innerHTML: '' };
result.search_mode = 'pubchem-mass';
result.formula = 'C2H6O';
result.formula_source = 'PubChem mass matches (no formula prediction)';
result.formula_hypotheses = [{ formula: 'C2H6O' }, { formula: 'C3H8' }];
Object.assign(result.candidates[0], {
  formula: 'C3H8', source: 'GLACIER', model_name: 'GLACIER',
  mass_error_ppm: -2.5, pubchem_cids: [123, 456],
});
vm.runInContext('renderEvidence()', context);
assert.match(elements.evidence.innerHTML, /Formula <b>C<sub>3<\/sub>H<sub>8<\/sub><\/b>/);
assert.match(elements.evidence.innerHTML, /Mass error: -2\.500 ppm/);
assert.match(elements.evidence.innerHTML, /compound\/123/);
assert.match(elements.evidence.innerHTML, /Other mass-matched formulas in this search/);
assert.doesNotMatch(elements.evidence.innerHTML, /rerun retrieval/);

const snapshot = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(
  root, 'demo/msms-structure-elucidation/source.json.gz'))));
for (const row of snapshot.results) {
  context.clinical = row.result;
  vm.runInContext('S.result = clinical; S.cand = 0; S.pairIdx = -1; S.mergedCache = new WeakMap();', context);
  const view = vm.runInContext('spectraFor(cand())', context);
  assert.equal(view.merged, true, row.label);
  assert.equal(view.pair.input_values.length, 5, row.label);
  assert.ok(view.exp.length > 0 && view.pred.length > 0 && view.e2p.size > 0, row.label);
  assert.ok(view.ids.some(id => id != null), row.label);
  for (const ref of view.predRefs.filter(Boolean)) {
    assert.equal(row.result.candidates[0].predicted_fragment_ids[ref.key][ref.index], ref.id);
  }
}

const navFocus = [];
elements.prev = { focus: () => navFocus.push('prev') };
elements.next = { focus: () => navFocus.push('next') };
context.location = { hash: '' };
vm.runInContext('S.index = [{}, {}, {}]; S.order = [0, 1, 2]; S.r = 1; stepUnknown(-1);', context);
assert.equal(context.location.hash, '#r0');
assert.equal(navFocus.at(-1), 'prev');
vm.runInContext('stepUnknown(1);', context);
assert.equal(context.location.hash, '#r2');
assert.equal(navFocus.at(-1), 'next');

context.document.querySelectorAll = () => [];
context.IntersectionObserver = class { disconnect() {} observe() {} };
for (const id of ['f-q', 'f-outcome', 'f-review', 'f-sim', 'ov-count', 'ov-pagination', 'ov-page-info', 'ov-page', 'ov-prev', 'ov-next']) {
  elements[id] = { value: '', innerHTML: '', textContent: '', disabled: false, hidden: false };
}
elements['f-sim'].value = '0';
elements['ov-table'] = { tBodies: [{ innerHTML: '' }] };
const tableRows = () => [...elements['ov-table'].tBodies[0].innerHTML.matchAll(/<tr data-i="(\d+)"/g)].map(m => Number(m[1]));
context.unknowns = Array.from({ length: 205 }, (_, index) => ({
  index, label: `unknown_${String(index).padStart(3, '0')}`, candidates: 0, outcome: 'no-candidates',
  spectrum_qc: { relative_intensities: [100, 50, 20, 10, 10], precursor_known: true },
}));
for (const count of [0, 1, 100, 101, 200, 205]) {
  context.count = count;
  vm.runInContext('S.index = unknowns.slice(0, count); S.page = 0; renderTable();', context);
  assert.equal(tableRows().length, Math.min(100, count));
  assert.equal(vm.runInContext('S.order.length', context), count, 'Navigation must retain all matching unknowns');
  assert.equal(elements['ov-pagination'].hidden, count <= 100);
  assert.equal(elements['ov-prev'].disabled, true);
  assert.equal(elements['ov-next'].disabled, count <= 100);
}
vm.runInContext('S.page = 1; renderTable();', context);
assert.deepEqual(tableRows(), Array.from({ length: 100 }, (_, i) => i + 100));
assert.equal(elements['ov-page-info'].textContent, 'Page 2 of 3 · 101–200 of 205 unknowns');
vm.runInContext('S.page = 2; renderTable();', context);
assert.deepEqual(tableRows(), [200, 201, 202, 203, 204]);
assert.equal(elements['ov-next'].disabled, true);
elements['f-q'].value = 'unknown_104';
vm.runInContext('renderTable(true);', context);
assert.deepEqual(tableRows(), [104], 'Search must find an unknown outside the first page');
assert.equal(elements['ov-page-info'].textContent, 'Page 1 of 1 · 1–1 of 1 unknowns');
elements['f-q'].value = 'missing';
vm.runInContext('renderTable();', context);
assert.equal(elements['ov-page-info'].textContent, 'Page 1 of 1 · 0–0 of 0 unknowns');
assert.deepEqual(tableRows(), []);
elements['f-q'].value = '';
vm.runInContext("S.sort = { key: 'label', dir: -1 }; renderTable(true);", context);
assert.equal(tableRows()[0], 204, 'Sorting must apply before pagination');
assert.equal(tableRows().at(-1), 105);

const qcFixture = { parentmass: 500, spectra: {
  '10': [[500, 10000], [499.995, 5000], [100, 100], [120, 10], [140, 10], [160, 10], [180, 10], [200, 9.999]],
}};
const beforeQC = JSON.stringify(qcFixture);
const qc = spectrumQC(qcFixture);
assert.deepEqual(qc.relative_intensities, [100, 10, 10, 10, 10, 9.999]);
assert.equal(JSON.stringify(qcFixture), beforeQC, 'QC must not mutate experimental spectra');
assert.deepEqual(spectrumQC({parentmass: 500, spectra: {
  '10': [[100, 100], [120, 6]], '20': [[100.001, 100], [140, 20]],
}}).relative_intensities, [100, 10, 3], 'Merge repeated fragments before setting the QC base peak');
assert.deepEqual(spectrumQC({parentmass: 500, spectra: {'10': [[500, 100]]}}).relative_intensities, []);
assert.deepEqual(spectrumQC({parentmass: 500, spectra: {}}).relative_intensities, []);
assert.equal(spectrumQC({spectra: {'10': [[100, 1]]}}).precursor_known, false);

context.qcCases = [
  { spectrum_qc: qc },
  { spectrum_qc: { relative_intensities: [100, 10, 10, 10, 9.999], precursor_known: true } },
  { spectrum_qc: { relative_intensities: [], precursor_known: true } },
  { spectrum_qc: { relative_intensities: [], precursor_known: false } },
];
assert.equal(vm.runInContext('passesQC(qcCases[0])', context), true);
for (let i = 1; i < 4; i++) assert.equal(vm.runInContext(`passesQC(qcCases[${i}])`, context), false);
vm.runInContext('S.qc.minIntensity = 100;', context);
assert.equal(vm.runInContext('qcPeakCount(qcCases[0])', context), 1);
vm.runInContext('S.qc.minPeaks = 1;', context);
assert.equal(vm.runInContext('passesQC(qcCases[0])', context), true);
vm.runInContext('S.qc.enabled = false;', context);
assert.equal(vm.runInContext('qcCases.every(passesQC)', context), true);

// QC applies before pagination and keeps failed unknowns out of keyboard navigation and search.
vm.runInContext("S.qc = { enabled: true, minPeaks: 5, minIntensity: 10 }; S.sort = { key: 'label', dir: 1 }; unknowns[0].spectrum_qc = qcCases[1].spectrum_qc; renderTable(true);", context);
assert.equal(tableRows()[0], 1);
assert.equal(vm.runInContext('S.order.length', context), 204);
assert.equal(vm.runInContext('S.order.includes(0)', context), false);
elements['pal-q'] = { value: 'unknown_000' };
assert.equal(vm.runInContext('palRows().length', context), 0);
vm.runInContext('S.qc.enabled = false; renderTable(true);', context);
assert.equal(tableRows()[0], 0);
assert.equal(vm.runInContext('palRows().length', context), 1);

const csvCandidates = Array.from({ length: 12 }, (_, i) => ({
  smiles: i === 0 ? 'F/C=C\\Cl' : 'C'.repeat(i + 1), entropy_similarity: i === 0 ? 0 : 0.9876543210123456,
}));
context.csvUnknowns = [
  { index: 0, label: 'unknown,"α"\n001', ranked_candidates: csvCandidates },
  { index: 1, label: 'no_candidates', ranked_candidates: [] },
  { index: 2, label: 'missing_score', ranked_candidates: [{ smiles: 'C |atomProp:0.note.a,b|' }] },
];
vm.runInContext('S.index = csvUnknowns; S.order = [2]; S.page = 1;', context);
const beforeCSV = JSON.stringify(context.csvUnknowns);
const csv = vm.runInContext('rankedSmilesCSV()', context);
const [csvHeader] = csv.split('\r\n');
assert.equal(csvHeader.split(',').length, 21);
assert.equal(csvHeader.split(',')[0], 'unknown_id');
assert.equal(csvHeader.split(',').at(-2), 'top10_smiles');
assert.equal(csvHeader.split(',').at(-1), 'top10_similarity');
assert.ok(csv.includes('"unknown,""α""\n001",F/C=C\\Cl,0,CC,0.9876543210123456'));
assert.ok(csv.includes('\r\nno_candidates' + ','.repeat(20) + '\r\n'));
assert.ok(csv.includes('\r\nmissing_score,"C |atomProp:0.note.a,b|"' + ','.repeat(19) + '\r\n'));
assert.ok(csv.includes('C'.repeat(10) + ',0.9876543210123456\r\n'));
assert.ok(!csv.includes('C'.repeat(11)), 'The CSV must stop after rank 10');
assert.equal(JSON.stringify(context.csvUnknowns), beforeCSV, 'CSV export must preserve saved rankings');
context.legacyCSV = { index: [{ index: 0, label: 'legacy' }], results: [{ result: {
  candidates: [{ smiles: 'CCO', entropy_similarity: 0.25 }],
} }] };
vm.runInContext('STATIC = legacyCSV; S.index = STATIC.index;', context);
assert.ok(vm.runInContext('rankedSmilesCSV()', context).includes('\r\nlegacy,CCO,0.25' + ','.repeat(18) + '\r\n'));
vm.runInContext('STATIC = null;', context);

context.experimental = {
  parentmass: 250, candidates: [], collision_unit: 'NCE',
  spectra: { '10.0': [[100, 1], [150, 0.5]], '20.0': [[100.001, 2], [200, 1]] },
  energy_mapping: [
    { input_value: 20, input_unit: 'NCE', collision_energy_ev: 10 },
    { input_value: 40, input_unit: 'NCE', collision_energy_ev: 20 },
  ],
};
vm.runInContext('S.result = experimental; S.cand = 0; S.pairIdx = -1; S.peak = null; S.mergedCache = new WeakMap();', context);
const experimental = vm.runInContext('spectraFor(cand())', context);
assert.equal(experimental.experimentalOnly, true);
assert.equal(experimental.merged, true);
assert.equal(experimental.exp.length, 3);
assert.equal(experimental.exp[0][1], 3);
assert.ok(Math.abs(experimental.exp[0][0] - (100 + 2 * 100.001) / 3) < 1e-9);
assert.equal(experimental.pred.length, 0);
assert.equal(experimental.e2p.size, 0, 'Experimental peaks must not acquire fabricated matches');
vm.runInContext('renderEnergy();', context);
assert.equal(elements.energy.value, '-1');
assert.match(elements.energy.innerHTML, /20 NCE/);
assert.match(elements.energy.innerHTML, /40 NCE/);
assert.doesNotMatch(elements.energy.innerHTML, /↔|ICEBERG/);
assert.equal(elements.compare.disabled, true);
assert.doesNotMatch(elements.legend.innerHTML, /matched|unexplained|Predicted/);
assert.match(vm.runInContext("peakTip(0, spectraFor(cand()), {kind: 'e', i: 0})", context), /No candidate prediction is available/);
vm.runInContext('S.pairIdx = 1;', context);
const individual = vm.runInContext('spectraFor(cand())', context);
assert.equal(individual.merged, false);
assert.equal(individual.key, '20.0');
assert.strictEqual(individual.exp, context.experimental.spectra['20.0']);
assert.equal(individual.pair.input_value, 40);
assert.equal(individual.pair.input_unit, 'NCE');

context.testPlot = { clientWidth: 800, innerHTML: '' };
vm.runInContext('wirePlot = () => {}; S.zoom = defaultRange(spectraFor(cand())); drawMirror(testPlot, 0, false);', context);
assert.match(context.testPlot.innerHTML, /aria-label="Experimental spectrum"/);
assert.match(context.testPlot.innerHTML, /data-s="e"/);
assert.doesNotMatch(context.testPlot.innerHTML, /data-s="p"|Predicted|similarity|NaN|Infinity/);

context.stepped = {
  parentmass: 250, candidates: [], spectra: { stepped: [[100, 10], [150, 5]] },
  acquisition_mode: 'stepped', collision_unit: 'NCE',
  stepped_collision: { values: [20, 40, 60], unit: 'NCE' },
};
vm.runInContext('S.result = stepped; S.pairIdx = -1; S.mergedCache = new WeakMap(); renderEnergy();', context);
assert.match(elements.energy.innerHTML, /stepped 20\/40\/60 NCE/);
assert.equal(elements.energy.disabled, true);
assert.equal(vm.runInContext('spectraFor(cand()).merged', context), false, 'A stepped spectrum is already one observation');
assert.strictEqual(vm.runInContext('spectraFor(cand()).exp', context), context.stepped.spectra.stepped);
vm.runInContext('S.result = { candidates: [], spectra: {} };', context);
assert.equal(vm.runInContext('spectraFor(cand())', context), null);
vm.runInContext("S.result = { candidates: [], spectra: { '10': [] } };", context);
assert.ok(vm.runInContext('defaultRange(spectraFor(cand())).every(Number.isFinite)', context));
