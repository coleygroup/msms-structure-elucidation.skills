---
name: msms-simulator
description: Predict MS/MS spectra for a given molecule (SMILES or InChI) using the in-house simulator model.
category: simulation
---

# msms-simulator

## Goal
Given a molecular structure (SMILES or InChI) and instrument parameters (precursor m/z, adduct, collision energy), predict the expected MS/MS spectrum using the in-house fragmentation simulator.

## Instructions

1. **Prepare input** — a SMILES string and instrument metadata.

2. **Run simulator**:
```bash
# Env: simulator
python .agents/skills/msms-simulator/scripts/run.py \
    --smiles "CC(=O)Oc1ccccc1C(=O)O" \
    --adduct "[M+H]+" \
    --collision_energy 35 \
    --output results/simulator_output.json
```

## Output Format
JSON file with keys:
- `smiles`: input structure
- `peaks`: list of `[mz, intensity]` pairs
- `metadata`: adduct, collision energy, model version

## Constraints
- **Environment**: `simulator` (`pixi run --environment simulator python ...`)
- **Input**: valid SMILES or InChI string
- **Checkpoint**: set `models.simulator.checkpoint` in `configs/default.yaml`

## References
- TODO: cite simulator model paper/repo once implemented.

---

**Author:** mlederbauer
**Contact:** [magled@mit.edu](mailto:magled@mit.edu)
