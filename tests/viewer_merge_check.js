/* Exercise the pure merge and the viewer's default merged state without a browser. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const zlib = require('node:zlib');

const root = path.resolve(__dirname, '..');
const { mergePeaks } = require(path.join(root, 'src/msms_structure_elucidation/visualize_assets/viewer_merge.js'));
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
