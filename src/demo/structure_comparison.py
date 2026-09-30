"""Coarse reference comparisons, not affinity or biological activity predictions."""
from __future__ import annotations

import math
import statistics

import gemmi


def chain_ca(model: gemmi.Model, name: str) -> tuple[str, list[gemmi.Position]]:
    """Require one unambiguous canonical protein chain with finite CA coordinates."""
    chains = [chain for chain in model if chain.name == name]
    if len(chains) != 1:
        raise ValueError(f"Expected exactly one chain {name}")
    sequence, positions = [], []
    for residue in chains[0]:
        code = gemmi.find_tabulated_residue(residue.name).one_letter_code
        atoms = [atom for atom in residue if atom.name == "CA"]
        if code not in "ACDEFGHIKLMNPQRSTVWY" or len(atoms) != 1:
            raise ValueError("Expected canonical residues with one CA each")
        atom = atoms[0]
        if atom.altloc not in ("\x00", " ") or not all(math.isfinite(v) for v in atom.pos):
            raise ValueError("Ambiguous or nonfinite CA coordinates")
        sequence.append(code)
        positions.append(atom.pos)
    if not positions:
        raise ValueError("Empty chain")
    return "".join(sequence), positions


def compare_complex(prediction: gemmi.Structure, reference: gemmi.Structure) -> dict:
    """Align chain A, measure chain B, and retain every NMR conformer's comparison."""
    if len(prediction) != 1 or len(reference) == 0:
        raise ValueError("Expected one prediction and a nonempty reference ensemble")
    target_seq, target = chain_ca(prediction[0], "A")
    peptide_seq, peptide = chain_ca(prediction[0], "B")
    if len(target) < 3:
        raise ValueError("Alignment requires at least three target residues")
    predicted_contacts = {(i, j) for i, a in enumerate(target) for j, b in enumerate(peptide)
                          if a.dist(b) < 8.0}
    rows = []
    for index, model in enumerate(reference):
        ref_target_seq, ref_target = chain_ca(model, "A")
        ref_peptide_seq, ref_peptide = chain_ca(model, "B")
        if (target_seq, peptide_seq) != (ref_target_seq, ref_peptide_seq):
            raise ValueError("Predicted and reference chain sequences differ")
        fit = gemmi.superpose_positions(ref_target, target)
        rmsd = math.sqrt(sum(math.dist(fit.transform.apply(a), b) ** 2
                             for a, b in zip(peptide, ref_peptide)) / len(peptide))
        contacts = {(i, j) for i, a in enumerate(ref_target) for j, b in enumerate(ref_peptide)
                    if a.dist(b) < 8.0}
        rows.append({"reference_model": index + 1, "target_ca_rmsd_angstrom": fit.rmsd,
                     "target_aligned_peptide_ca_rmsd_angstrom": rmsd,
                     "reference_ca_contacts": len(contacts),
                     "reference_contact_recall": len(contacts & predicted_contacts) / len(contacts)
                     if contacts else None})
    values = [row["target_aligned_peptide_ca_rmsd_angstrom"] for row in rows]
    return {"method": "target-aligned peptide CA RMSD; CA contacts <8 Angstrom",
            "peptide_rmsd_angstrom": {"min": min(values), "median": statistics.median(values),
                                     "max": max(values)}, "conformers": rows}
