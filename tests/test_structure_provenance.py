import runpy
from pathlib import Path

import pytest

from test_structure_comparison import structure
from vatlog import VatRecorder
from vatlog.provenance import verify_provenance
from vatlog.roles import Keyring


def test_recorded_import_compare_report_preserves_real_structure_metric(tmp_path):
    functions = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/structure_provenance.py'))
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        load = functions['import_structure'].with_recorder(recorder)
        compare = functions['compare_structures'].with_recorder(recorder)
        report = functions['comparison_report'].with_recorder(recorder)
        reference = load.call(structure().make_mmcif_document().as_string().encode(), 'reference.cif')
        prediction = load.call(structure(peptide_shift=20).make_mmcif_document().as_string().encode(), 'prediction.cif')
        result = compare.call(prediction.ref('/bytes'), reference.ref('/bytes'))
        assert result.value['peptide_rmsd_angstrom']['median'] == pytest.approx(20)
        assert result.value['conformers'][0]['reference_contact_recall'] == 0
        output = report.call([result.ref()])
        assert output.value['count'] == 1
        assert output.value['comparisons'][0] == result.value
        assert len(result.node.statement().input_bindings) == 2
        assert verify_provenance(recorder.evidence_nodes(), recorder.trust).outcome == 'VALID'
        assert reference.value['boundary'].startswith('saved structure supplied by operator')
