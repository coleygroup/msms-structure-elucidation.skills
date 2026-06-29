# Example: 2-aminoethyl benzoate

## Goal
Predict the MS/MS spectrum of 2-aminoethyl benzoate (`c1ccccc1C(=O)OCCN`) at CE=20 and 40 eV, [M+H]+, using ICEBERG.

## Steps
```bash
# Env: simulator-iceberg
python .agents/skills/msms-sim-iceberg/examples/example-2-aminoethyl-benzoate/run_example.py \
    --gen_ckpt downloads/iceberg_dag_gen_msg_best.ckpt \
    --inten_ckpt downloads/iceberg_dag_inten_msg_best.ckpt
```

## Expected Output
- Precursor `[M+H]+` ≈ 166.0868 Da
- `spectrum.png` — two-panel stem plot (20 eV + 40 eV)
- `fragments.json` — fragment SMILES per peak at each CE
- `input_configs.yaml` — run parameters

## Validation
Compare predicted dominant fragments against the MassBank reference spectrum for 2-aminoethyl benzoate.
