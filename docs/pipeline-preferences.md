# Saved pipeline choices and answers

Read the effective configuration and saved answers before asking a pipeline question. Shared defaults are in `configs/default.yaml`; user choices and confirmations go in the ignored `configs/local.yaml`. Use `--config path/to/config.yaml` on the command below for another project-local config. Do not commit private host settings, file paths or user confirmations.

## First feature-finding run

If the user supplied a feature list, feature table/MGF, or selected spectra, use those inputs and skip the feature-finder question. Otherwise look for a project-scoped `feature_finder` answer. When none exists, ask once:

1. **GNPS2 Everything Bagel (recommended)** — use the GNPS2 hosted or browser interface.
2. **msconvert + MZmine** — convert vendor raw data when needed, then run local MZmine.
3. **My own workflow** — follow the supplied workflow or accept its feature table and spectra.

A shared default recommends GNPS2 Everything Bagel; it does not count as the user's confirmation. After the user chooses, save the operational setting and the answer together. For example:

```bash
msms-structure-elucidation preferences \
  --set feature_detect.backend everything_bagel \
  --answer feature_finder everything_bagel
```

Use `mzmine` or `custom` for the other choices. For a custom route, collect only the instructions/output details still needed and save them under `feature_detect.custom.instructions`. If the user chooses a GNPS2 Everything Bagel interface, save `feature_detect.everything_bagel.interface` as `browser` or `gnps2`. With `auto`, select an appropriate interface from data location and current resource limits. `msms-inspect` is optional; preprocessing is needed only when the selected route cannot accept the files or requires polarity splitting.

On later runs, reuse the saved choice without asking again. Ask only for missing details, changed requirements or a conflict with the current inputs. A new explicit user choice takes precedence; update the settings and confirmation accordingly.

## Formula search

Follow [formula search](formula-search.md) when no formula was supplied. Recommend BUDDY's top three formulas and let the user choose that route or PubChem mass filtering. Save `formula_strategy` (`buddy_top3` or `pubchem_mass`), `mass_tolerance` and `formula_elements` with a dataset/analysis scope and matching input context. A shared default is not confirmation. Reuse an explicit request or saved choice; if BUDDY returns no formulas, ask before switching to mass filtering.

## Optional de novo route after evidence review

Use `msms-evidence-review` for the assessment. Review delegation is an operational setting, separate from the user's scientific route choice. `pipeline.evidence_review.mode` accepts `auto` (delegate independent batch work when supported), `main` (direct review), or `delegate` (prefer delegation, with direct fallback). `max_reviewers` and `features_per_task` are positive integers, defaulting to two and one; actual concurrency is bounded by available slots and ready work while the main agent keeps monitoring. Save overrides in the ignored local config, for example:

```bash
msms-structure-elucidation preferences \
  --set pipeline.evidence_review.mode auto \
  --set pipeline.evidence_review.max_reviewers 2 \
  --set pipeline.evidence_review.features_per_task 1
```

Reuse these settings without repeated questions. They guide agent dispatch; they do not start an LLM pool inside the Python batch worker or authorize proposals/model runs.

After retrieval/scoring, the agent reviews every result. For weakly supported entries, ask the user whether to use agent proposals scored by ICEBERG/GLACIER (workflow step 4), FRIGID de novo followed by scoring (step 5), both, or skip. Wait for the selection before either optional route. One answer can cover an identified batch or a subset of entries. Reuse an earlier selection only if it explicitly covers the same analysis, inputs, affected entries and model context.

Save the selected answer as `denovo_route` with value `agent`, `frigid`, `both` or `skip`, using an analysis/entry scope and context identifying those inputs and model/weights. Keep the agent's recommended action distinct from the user-selected route in `evidence_review.json`. Do not create a project-wide de novo authorization from one analysis's answer. Selecting both requests both routes; selecting skip proceeds to reporting with the unresolved evidence visible.

## Save and reuse other answers

```bash
# Show preferences and retrieve the saved project-level feature-finder answer.
msms-structure-elucidation preferences --get feature_finder

# Save a provider-confirmed unit for one dataset, identified by its manifest hash.
msms-structure-elucidation preferences \
  --answer collision_unit eV --scope dataset-01 \
  --context-json '{"manifest_sha256":"<actual-manifest-sha256>"}'

# Retrieve it only for the same dataset and context.
msms-structure-elucidation preferences \
  --get collision_unit --scope dataset-01 \
  --context-json '{"manifest_sha256":"<actual-manifest-sha256>"}'
```

For settings established from raw/mzML acquisition records, use `--source metadata` with an input/scan-specific context; retain the actual source parameters and units. The default `--source user` is for user answers. Both verified sources can be reused in their exact context.

The command merges updates and keeps unrelated config values. `pipeline.answers.<scope>.<question>` holds the answer, its `source` (`user` or `metadata`) and its context. Values may be JSON or unquoted strings. Reading preferences does not create a config file. `pipeline.reuse_answers: false` disables answer reuse.

Save genuine user answers or settings established from explicit acquisition metadata, with accurate provenance. Do not save guesses or suggested defaults as verified answers. Project-scoped answers suit workflow choices. Collision units, energy values and formulas require a dataset/spectrum scope and non-empty context; never apply one dataset's scientific answers to an unrelated dataset. Use input/manifest hashes to identify that context. Keep an analysis-specific search mode, ppm tolerance, adduct and instrument in its own scope unless the user explicitly establishes a broader preference.

For `model_selection`, include the host, model and exact checkpoint identities in the context, and save actual paths under `models.simulator`. Reuse that confirmation while those assets and the host match; ask again when they change. A matching file path alone does not prove unchanged weights. Existing execution/Slurm settings stay in their normal config sections and should not be requested repeatedly.

Saved answers guide the agent. Model commands still require explicit scientific inputs such as `--collision-unit`; pass the validated answer to the command or MCP tool. If a saved answer is absent, its context differs or metadata conflicts, resolve that issue with the user before dependent work.
