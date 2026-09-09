import numpy as np

from fina_risk.hybrid import hybrid_delta, select_method


def test_method_selector_prefers_aad_for_stable_delta() -> None:
    assert select_method("delta", transition_fraction=0.0) == "AAD_FIXED_BRANCH"
    assert select_method("delta", transition_fraction=0.1) == "AAD_FIXED_BRANCH+PATHWISE"
    assert select_method("gamma", transition_fraction=0.0) == "PATHWISE"
    assert select_method("bucket_vega", transition_fraction=0.1) == "CRN_BUCKET_FD"


def test_hybrid_delta_reports_transition_fallback() -> None:
    terminal = np.asarray([[110.0, 90.0], [100.0, 100.0], [80.0, 120.0]])
    result = hybrid_delta(terminal, np.asarray([100.0, 100.0]), np.asarray([100.0, 100.0]), 0.8, 1.0)
    assert result["aad_available"] in {True, False}
    assert result["transition_path_fraction"] > 0.0
    assert result["pathwise_fallback"] is True
    assert "PATHWISE" in result["method"] or result["method"] == "PATHWISE"
