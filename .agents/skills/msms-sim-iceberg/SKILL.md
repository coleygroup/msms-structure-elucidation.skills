---
name: msms-sim-iceberg
description: Predict LC-MS/MS spectra and fragment assignments from SMILES with ICEBERG when a precomputed public atlas spectrum is unavailable.
---
# ICEBERG simulation

Use public atlas retrieval first for ordinary PubChem structures. For a candidate missing from the atlas, clone [coleygroup/ms-pred](https://github.com/coleygroup/ms-pred) if no checkout exists, read that checkout's `README.md` **Install & setup** section, and follow its CPU or CUDA environment instructions for the host. Verify the resulting interpreter before simulation. `bash setup_envs.sh` is only a CPU venv helper; check the upstream README first. Then supply ICEBERG generator and intensity checkpoints. Open-source MassSpecGym weights are linked in the [ms-pred README](https://github.com/coleygroup/ms-pred#pretrained-iceberg-21-model-weights-on-massspecgym). A user with a NIST license can provide local NIST-trained checkpoints; do not distribute them.

For an experimental spectrum and proposed SMILES, use the portable review command:

```bash
msms-structure-elucidation review --result results/sample/retrieval.json \
  --smiles-json results/sample/proposals.json --model iceberg \
  --gen-checkpoint /path/to/gen.ckpt --inten-checkpoint /path/to/inten.ckpt
```

For simulation alone, run the existing ms-pred adapter with the model interpreter:

```bash
"${MS_PRED_PYTHON:-python}" .agents/skills/msms-sim-iceberg/scripts/predict_msms.py \
  --smiles 'CCO' --gen_ckpt /path/to/gen.ckpt --inten_ckpt /path/to/inten.ckpt \
  --collision_energies 20 40 --output_dir results/iceberg
```

The adapter accepts integer eV values and calls ICEBERG with `nce=False`. If the source is experimental NCE, confirm that unit with the provider, convert using precursor m/z, and round to integer eV before invoking it. Validate collision energies and adduct before comparing predicted and experimental spectra. Similarity is not calibrated confidence.
