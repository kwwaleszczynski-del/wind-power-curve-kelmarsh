import numpy as np
import pandas as pd
import pytest

from wind_power_curve import power_curve as pc


def test_air_density_standard_conditions():
    assert pc.air_density(15.0, pressure_pa=101325.0) == pytest.approx(1.225, abs=1e-3)
    # standard atmosphere: about 1.2 % less pressure per 100 m
    assert pc.standard_pressure(100.0) == pytest.approx(100129, rel=1e-3)
    with pytest.raises(ValueError):
        pc.air_density(15.0)


def test_normalisation_cube_root():
    assert pc.normalise_wind_speed(10.0, pc.RHO_0) == pytest.approx(10.0)
    # 8 x denser air carries the power of 2 x the wind speed
    assert pc.normalise_wind_speed(10.0, 8 * pc.RHO_0) == pytest.approx(20.0)


def test_bins_centred_on_half_metres_and_min_count():
    ws = np.array([4.74, 4.76, 5.0, 5.2, 5.26, 9.0])
    p = np.array([100, 200, 300, 400, 500, 1000.0])
    curve = pc.bin_power_curve(ws, p, min_count=1)
    assert list(curve.index) == [4.5, 5.0, 5.5, 9.0]
    assert curve.loc[5.0, "n"] == 3  # 4.76, 5.0 and 5.2 fall in [4.75, 5.25)
    assert curve.loc[5.0, "power_kw"] == pytest.approx(300.0)
    assert list(pc.bin_power_curve(ws, p, min_count=3).index) == [5.0]


def test_interpolate_holds_last_value_until_cut_out():
    p = pc.interpolate([3, 4, 5], [0, 100, 200], [2.0, 3.5, 5.0, 20.0, 26.0, np.nan])
    assert p[:5].tolist() == [0.0, 50.0, 200.0, 200.0, 0.0]
    assert np.isnan(p[5])


def test_aep_of_constant_power_is_power_times_probability():
    # constant 1000 kW from 0 to cut-out: AEP = 8760 h * 1 MW * P(V <= 25)
    aep = pc.aep_rayleigh([0.0, 30.0], [1000.0, 1000.0], mean_ws=7.0)
    p_below_cut_out = 1 - np.exp(-np.pi / 4 * (25 / 7) ** 2)
    assert aep == pytest.approx(8760 * p_below_cut_out, rel=1e-3)


def test_aep_increases_with_mean_wind():
    a6 = pc.aep_rayleigh(pc.MM92_CURVE.index, pc.MM92_CURVE.values, 6.0)
    a8 = pc.aep_rayleigh(pc.MM92_CURVE.index, pc.MM92_CURVE.values, 8.0)
    assert 3000 < a6 < a8 < 2.05 * 8760


def test_database_completeness():
    ws = np.arange(1.0, 20.0, 0.5)
    curve = pd.DataFrame({"ws": ws, "power_kw": pc.interpolate(pc.MM92_CURVE.index, pc.MM92_CURVE.values, ws),
                          "n": 100}, index=ws)
    assert pc.database_complete(curve, pc.RATED_KW)
    assert not pc.database_complete(curve.drop(index=6.0), pc.RATED_KW)
    assert not pc.database_complete(curve.assign(n=3), pc.RATED_KW)  # far below 180 h
