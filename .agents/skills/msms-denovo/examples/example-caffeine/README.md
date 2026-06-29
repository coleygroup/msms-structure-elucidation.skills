# Example: Caffeine de novo elucidation

Caffeine (C8H10N4O2, MW 194.08) is a well-characterized purine alkaloid.
This example tests the full denovo pipeline on a known compound so you can
verify that the top candidate recovers the correct structure.

**Expected top candidate SMILES:** `Cn1cnc2c1c(=O)n(c(=O)n2C)C`
**Precursor [M+H]+:** 195.0882 Da

## Run

```bash
# Step 1 — assign subformulae
pixi run --environment denovo python .agents/skills/msms-subformulae/scripts/run.py \
    --spectrum .agents/skills/msms-denovo/examples/example-caffeine/caffeine.ms \
    --formula C8H10N4O2 \
    --adduct "[M+H]+" \
    --output-dir .agents/test/example-caffeine/subformulae/

# Step 2 — de novo prediction
pixi run --environment denovo python .agents/skills/msms-denovo/scripts/run.py \
    --spectrum .agents/skills/msms-denovo/examples/example-caffeine/caffeine.ms \
    --formula C8H10N4O2 \
    --adduct "[M+H]+" \
    --subform-dir .agents/test/example-caffeine/subformulae/ \
    --top-k 10 \
    --output .agents/test/example-caffeine/denovo_output.json
```

Output is written to `.agents/test/example-caffeine/`.
