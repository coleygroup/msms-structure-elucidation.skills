# Choose formula prediction or mass filtering

When a formula or formula list is supplied, use it unless the user explicitly requests a mass-only search. Otherwise offer these two routes once for the current analysis, recommending **BUDDY top three**:

| Choice | Search |
| --- | --- |
| BUDDY top three (recommended) | Infer up to three ranked formulas with BUDDY/MSBuddy, then search candidate structures for each. |
| PubChem mass filtering | Skip formula prediction. Search the adduct-corrected neutral monoisotopic mass within the selected ppm tolerance, retain every eligible matched formula and structure, and rank them together. |

`pipeline.formula_search.strategy: buddy_top3` is a recommendation, not a user confirmation. Reuse an explicit request or a matching saved `formula_strategy` answer; otherwise ask before taking either route. Save `buddy_top3` or `pubchem_mass` with a dataset/analysis scope and input context under [pipeline preferences](pipeline-preferences.md). Save the selected `mass_tolerance` and `formula_elements` in the same scope. Do not ask again for unchanged inputs and an already selected route.

For BUDDY, use the returned ranking and search at most three distinct formulas. Record them as inferred hypotheses, not established identities. If no formula is returned, explain the outcome and offer mass filtering; do not silently switch routes. With an ICEBERG selection, prefer suitable atlas spectra. With GLACIER, retrieve the PubChem structures for each selected formula and predict them with the confirmed GLACIER weights. `run` and `batch` support this formula-guided GLACIER route without `--mass-only`.

With MCP connected, call `server_info` first. After the user selects BUDDY, use `infer_formulas` to obtain up to three hypotheses without launching predictions. For GLACIER, pass the selected formulas to `search_pubchem_formulas`; for ICEBERG, retrieve the selected formulas with `retrieve_atlas`. The user-selected mass-filtering route uses `search_pubchem_mass`. Poll every returned job ID and apply the evidence-review workflow.

## Mass-search elements and efficiency

The default allowed elements are **C, H, N, O, P, S, F, Cl, Br, I, Si, B and Se** (`CHNOPSClBrFISiBSe`). Override them with `--formula-elements` or the MCP `elements` argument. Keep the actual element filter, ppm window, adduct, exclusions and candidate counts in the result. Check support against the selected model/checkpoint before predicting elements outside its feature alphabet; report unsupported structures explicitly.

The search uses [PubChem's indexed monoisotopic-mass range query](https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest-tutorial#by-mass), followed by property requests in batches of 500 CIDs and local element, charge, isotope and single-component filters. It does not enumerate combinations of elemental formulas. Broadening the element list adds eligible candidates to predict; it does not add a separate mass query per element. Do not hide a candidate cap or silently narrow the ppm/element settings to reduce work.

For example, after the user selects mass filtering and confirms these settings:

```bash
msms-structure-elucidation run --input sample.ms --collision-unit NCE \
  --mass-only --ms1-ppm 5 --formula-elements CHNOPSClBrFISiBSe \
  --model glacier --output-dir results/sample
```

Reuse matching PubChem candidate lists when available, preserving the original query and filters. Reuse compatible **raw predictions** across features through the [shared prediction cache](technical-guide.md#shared-predictions). Always score each unknown against its own measured spectra and review its evidence separately.
