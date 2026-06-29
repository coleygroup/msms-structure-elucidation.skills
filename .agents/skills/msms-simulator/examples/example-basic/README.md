# Example: Predict spectrum for aspirin

## Goal
Predict the MS/MS spectrum of aspirin (acetylsalicylic acid) at CE=35 eV, [M+H]+.

## Steps
```bash
# Env: simulator
python .agents/skills/msms-simulator/scripts/run.py \
    --smiles "CC(=O)Oc1ccccc1C(=O)O" \
    --adduct "[M+H]+" \
    --collision_energy 35 \
    --output examples/example-basic/output.json
```

## Expected Output
`output.json` with predicted peaks. Compare against NIST or MassBank reference spectrum for aspirin.

## TODO
Implement model and add expected peak list for validation.
