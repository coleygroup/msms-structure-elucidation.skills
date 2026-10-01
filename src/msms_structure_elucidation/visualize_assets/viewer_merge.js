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
  const api = { mergePeaks };
  root.MSMSMerge = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
