"""cerberus.baseline holds the analysis drives, solar_path and learn share.
drives re-exports every moved name, so existing ``drives.<name>`` callers and
config defaults keep working."""

import pytest

from cerberus import baseline, drives, learn, solar_path

MOVED = ("DEFAULT_EPOCHS", "MIN_BASELINE", "SHORE_INPUT_W", "Z_FLAG", "_integrate",
         "_median", "_slope", "electrical_series", "epoch_start", "robust_z")


def test_drives_re_exports_the_moved_names_unchanged():
    for name in MOVED:
        assert getattr(drives, name) is getattr(baseline, name), name


def test_solar_path_and_learn_use_baseline_for_the_shared_analysis():
    assert solar_path.baseline is baseline and learn.baseline is baseline


def test_robust_z_floors_a_flat_history():
    z, med, mad = baseline.robust_z(10.2, [10.0] * 8)
    assert med == 10.0 and mad == pytest.approx(0.2) and z == pytest.approx(1.0)   # 2% of the median
