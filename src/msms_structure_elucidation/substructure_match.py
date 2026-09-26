"""Substructure / functional-group comparison between a candidate molecule
and a ground-truth molecule, for cases where whole-molecule Tanimoto is low
but the candidate may still share meaningful chemistry with the truth."""

from __future__ import annotations

from dataclasses import dataclass, field

from rdkit import Chem
from rdkit.Chem import rdFMCS, rdRascalMCES

# ponytail: a fixed, common set of functional groups -- covers the usual
# organic-chemistry vocabulary without trying to be exhaustive. Add more
# SMARTS here if a specific group matters for a dataset.
FUNCTIONAL_GROUPS: dict[str, str] = {
    "carboxylic_acid": "[CX3](=O)[OX2H1]",
    "ester": "[#6][CX3](=O)[OX2H0][#6]",
    "amide": "[CX3](=[OX1])[NX3]",
    "primary_amine": "[NX3;H2;!$(NC=O)]",
    "secondary_amine": "[NX3;H1;!$(NC=O)]",
    "tertiary_amine": "[NX3;H0;!$(NC=O);!$(N=*)]",
    "aromatic_ring": "[a]",
    "pyridine": "c1ccncc1",
    "nitro": "[NX3](=O)=O",
    "halogen": "[F,Cl,Br,I]",
    "hydroxyl": "[OX2H][#6;!$([#6]=O)]",
    "ether": "[OD2]([#6])[#6]",
    "ketone": "[#6][CX3](=O)[#6]",
    "aldehyde": "[CX3H1](=O)[#6]",
    "nitrile": "[NX1]#[CX2]",
    "sulfonamide": "[#16X4](=[OX1])(=[OX1])([NX3])",
    "urea": "[NX3][CX3](=[OX1])[NX3]",
    "carbamate": "[NX3][CX3](=[OX1])[OX2]",
}
_COMPILED_GROUPS = {
    name: Chem.MolFromSmarts(smarts) for name, smarts in FUNCTIONAL_GROUPS.items()
}


@dataclass
class SubstructureComparison:
    mcs_smarts: str | None
    mcs_num_atoms: int
    mcs_num_bonds: int
    mcs_frac_of_candidate: float  # MCS size / candidate's own atom count
    mcs_frac_of_truth: float  # MCS size / true molecule's own atom count
    # MCES (Maximum Common Edge Subgraph, via RDKit's RascalMCES) -- a
    # bond/connectivity-based analogue to the atom-based MCS above, and the
    # more standard "did we learn the right graph structure" metric in
    # cheminformatics (used e.g. in MassSpecGym-style benchmarks).
    mces_smarts: str | None = None
    mces_num_bonds: int = 0
    mces_similarity: float = 0.0  # RascalMCES's own Johnson-style similarity, in [0, 1]
    mces_timed_out: bool = False
    shared_groups: list[str] = field(default_factory=list)
    candidate_only_groups: list[str] = field(default_factory=list)
    truth_only_groups: list[str] = field(default_factory=list)


def _functional_groups_present(mol) -> set[str]:
    present = set()
    for name, pattern in _COMPILED_GROUPS.items():
        if pattern is not None and mol.HasSubstructMatch(pattern):
            present.add(name)
    return present


def compare_substructures(
    candidate_smiles: str,
    truth_smiles: str,
    mcs_timeout: int = 5,
) -> SubstructureComparison | None:
    """Compare a candidate molecule against the ground truth: Maximum Common
    Substructure (as a fraction of each molecule's size) and shared/unique
    functional groups. Returns None if either SMILES fails to parse."""
    cand_mol = Chem.MolFromSmiles(candidate_smiles)
    truth_mol = Chem.MolFromSmiles(truth_smiles)
    if cand_mol is None or truth_mol is None:
        return None

    mcs_result = rdFMCS.FindMCS(
        [cand_mol, truth_mol],
        timeout=mcs_timeout,
        bondCompare=rdFMCS.BondCompare.CompareOrderExact,
        atomCompare=rdFMCS.AtomCompare.CompareElements,
    )

    # RascalMCES returns [] (not an exception) when no match clears its
    # internal similarity threshold -- default options are tuned for
    # "reasonably similar" pairs, so a wide-open threshold is used here to
    # always get a best-effort answer, even for very dissimilar molecules.
    mces_opts = rdRascalMCES.RascalOptions()
    mces_opts.similarityThreshold = 0.0
    mces_opts.timeout = mcs_timeout
    mces_results = rdRascalMCES.FindMCES(cand_mol, truth_mol, mces_opts)
    mces_result = mces_results[0] if mces_results else None

    n_cand = cand_mol.GetNumHeavyAtoms()
    n_truth = truth_mol.GetNumHeavyAtoms()

    cand_groups = _functional_groups_present(cand_mol)
    truth_groups = _functional_groups_present(truth_mol)

    return SubstructureComparison(
        mcs_smarts=mcs_result.smartsString if not mcs_result.canceled else None,
        mcs_num_atoms=mcs_result.numAtoms,
        mcs_num_bonds=mcs_result.numBonds,
        mcs_frac_of_candidate=mcs_result.numAtoms / n_cand if n_cand else 0.0,
        mcs_frac_of_truth=mcs_result.numAtoms / n_truth if n_truth else 0.0,
        mces_smarts=mces_result.smartsString if mces_result else None,
        mces_num_bonds=len(mces_result.bondMatches()) if mces_result else 0,
        mces_similarity=mces_result.similarity if mces_result else 0.0,
        mces_timed_out=mces_result.timedOut if mces_result else False,
        shared_groups=sorted(cand_groups & truth_groups),
        candidate_only_groups=sorted(cand_groups - truth_groups),
        truth_only_groups=sorted(truth_groups - cand_groups),
    )


def demo() -> None:
    """ponytail: self-check -- known case with a real shared scaffold."""
    truth = "CC(=O)Oc1ccccc1C(=O)O"  # aspirin
    candidate = "CC(=O)Oc1ccccc1C(=O)N"  # aspirin amide analog
    result = compare_substructures(candidate, truth)
    assert result is not None
    assert result.mcs_num_atoms >= 9, (
        f"expected large shared scaffold, got {result.mcs_num_atoms} atoms"
    )
    assert result.mces_similarity > 0.5, (
        f"expected high MCES similarity, got {result.mces_similarity}"
    )
    assert "aromatic_ring" in result.shared_groups
    assert "ester" in result.shared_groups
    print("demo OK:", result)


if __name__ == "__main__":
    demo()
