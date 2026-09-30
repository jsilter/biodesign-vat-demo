import math

import gemmi
import pytest

from demo.structure_comparison import compare_complex


def structure(shift=0.0, peptide_shift=0.0, rotate=False):
    result = gemmi.Structure()
    model = gemmi.Model("1")
    for name, coords in [("A", [(0, 0, 0), (4, 0, 0), (0, 4, 0), (0, 0, 4)]),
                         ("B", [(1, 1, 1), (2, 1, 1)])]:
        chain = gemmi.Chain(name)
        for index, (x, y, z) in enumerate(coords):
            residue = gemmi.Residue()
            residue.name = "ALA"
            residue.seqid = gemmi.SeqId(index + 1, " ")
            atom = gemmi.Atom()
            atom.name = "CA"
            if rotate:
                x, y = -y, x
            atom.pos = gemmi.Position(x + shift + (peptide_shift if name == "B" else 0), y, z)
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    result.add_model(model)
    return result


def test_translation_preserves_interface_and_target_aligned_peptide():
    result = compare_complex(structure(shift=20, rotate=True), structure())
    assert result["peptide_rmsd_angstrom"]["max"] == pytest.approx(0, abs=1e-6)
    assert result["conformers"][0]["reference_contact_recall"] == 1


def test_displaced_peptide_is_not_independently_realigned():
    result = compare_complex(structure(peptide_shift=20), structure())
    assert result["peptide_rmsd_angstrom"]["median"] == pytest.approx(20)
    assert result["conformers"][0]["reference_contact_recall"] == 0


def test_all_reference_conformers_are_reported():
    reference = structure()
    reference.add_model(structure(peptide_shift=10)[0])
    result = compare_complex(structure(), reference)
    assert len(result["conformers"]) == 2
    assert result["peptide_rmsd_angstrom"]["median"] == pytest.approx(5)


@pytest.mark.parametrize("bad", ["sequence", "nan", "missing_chain", "extra_model"])
def test_invalid_correspondence_is_rejected(bad):
    prediction = structure()
    if bad == "sequence":
        prediction[0]["B"][0].name = "CYS"
    elif bad == "nan":
        prediction[0]["A"][0][0].pos.x = math.nan
    elif bad == "missing_chain":
        del prediction[0]["B"]
    else:
        prediction.add_model(prediction[0])
    with pytest.raises(ValueError):
        compare_complex(prediction, structure())
