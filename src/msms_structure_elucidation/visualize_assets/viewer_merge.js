/* Copyright (c) 2026 Coley Group. */
'use strict';

// Match the ms-pred WebUI mirror plot: sum intensities and use an
// intensity-weighted m/z centroid for peaks grouped within 20 ppm.
(function (root) {
  function mergePeaks(spectra, tolerancePpm = 20) {
    const all = [];
    for (const spectrum of spectra) {
      (spectrum.peaks || []).forEach((peak, index) => {
        const mz = Number(peak[0]), intensity = Number(peak[1]);
        if (!Number.isFinite(mz) || mz <= 0 || !Number.isFinite(intensity) || intensity <= 0) return;
        all.push({ mz, intensity, key: String(spectrum.key), index,
          id: spectrum.ids?.[index] ?? null });
      });
    }
    all.sort((a, b) => a.mz - b.mz);
    const peaks = [], members = [];
    let current = null;
    for (const peak of all) {
      if (current && Math.abs(peak.mz - current.mz) / current.mz * 1e6 <= tolerancePpm) {
        const total = current.intensity + peak.intensity;
        current.mz = (current.mz * current.intensity + peak.mz * peak.intensity) / total;
        current.intensity = total;
        current.members.push(peak);
      } else {
        if (current) { peaks.push([current.mz, current.intensity]); members.push(current.members); }
        current = { mz: peak.mz, intensity: peak.intensity, members: [peak] };
      }
    }
    if (current) { peaks.push([current.mz, current.intensity]); members.push(current.members); }
    return { peaks, members };
  }
  function spectrumQC(result) {
    const precursor = Number(result.parentmass);
    if (!Number.isFinite(precursor) || precursor <= 0) return { relative_intensities: [], precursor_known: false };
    const spectra = Object.entries(result.spectra || {}).map(([key, peaks]) => ({ key,
      peaks: peaks.filter(p => Math.abs(p[0] - precursor) / precursor * 1e6 > 20) }));
    const intensities = mergePeaks(spectra).peaks.map(p => p[1]);
    const base = intensities.reduce((max, value) => Math.max(max, value), 0);
    return { relative_intensities: intensities.map(v => 100 * (v / base)).sort((a, b) => b - a), precursor_known: true };
  }
  const api = { mergePeaks, spectrumQC };
  root.MSMSMerge = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
