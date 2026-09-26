---
name: msms-atlas-lookup
description: Batch lookup predicted spectra by SMILES from a locally mounted ICEBERG atlas when that atlas is available.
---
# Local atlas lookup by SMILES

This optional batch path looks up SMILES in a locally mounted, indexed MGF atlas and writes HDF5. It is useful when a licensed private atlas mount is available. The default public PubChem atlas workflow is `msms-retrieval`: it downloads formula MGFs through `https://iceberg-ms.mit.edu/download_mgf` and ranks experimental `.ms` spectra locally with ms-pred. The public endpoint excludes licensed NIST structures.

```bash
"${MS_PRED_PYTHON:-python}" .agents/skills/msms-atlas-lookup/scripts/run.py \
  --input-csv molecules.csv --smiles-col smiles --adduct '[M+H]+' \
  --atlas-dir /path/to/local/atlas --checkpoint-dir results/atlas/checkpoints \
  --output results/atlas/spectra.hdf5 --failure-log results/atlas/failures.log
```

The local mount must contain `h_plus_out_mgf/` or `h_minus_out_mgf/` with per-formula MGF and `.idx` sidecars. `--limit` bounds a probe. Missing structures and invalid SMILES are recorded in the failure log; a miss is not a zero similarity. Keep licensed NIST files on authorized storage and do not publish them.
