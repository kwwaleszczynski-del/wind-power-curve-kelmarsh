import numpy as np
import pandas as pd
import pytest

from conftest import START, empty_status, make_farm
from wind_power_curve import performance as pf
from wind_power_curve.pipeline import prepare
from wind_power_curve.power_curve import MM92_CURVE, interpolate

BASELINE = (START, START + pd.Timedelta(days=31))
FEB = pd.Timestamp("2017-02-01", tz="UTC")


def _static(n):
    return pd.DataFrame({"elevation_m": 0.0, "hub_height_m": 0.0, "cod": START}, index=pd.Index(range(1, n + 1), name="turbine"))


@pytest.fixture(scope="module")
def scenario():
    """6 turbines, 90 days, changes from February: T3 loses 10 % of its power (a real fault); T4's anemometer reads
    5 % low while the turbine is fine; T5 gets 5 % less wind (a flow effect, the turbine is fine).
    T1 is stopped for one day in March."""
    farm = make_farm(n_turbines=6, days=90)
    ts = farm.index.get_level_values("timestamp")
    t = farm.index.get_level_values("turbine")
    farm.loc[(t == 3) & (ts >= FEB), "power_kw"] *= 0.9
    farm.loc[(t == 4) & (ts >= FEB), "ws"] *= 0.95
    t5 = (t == 5) & (ts >= FEB)
    farm.loc[t5, "ws"] *= 0.95
    farm.loc[t5, "power_kw"] = interpolate(MM92_CURVE.index, MM92_CURVE.values, farm.loc[t5, "ws"])
    stop = (pd.Timestamp("2017-03-10", tz="UTC"), pd.Timestamp("2017-03-11", tz="UTC"))
    farm.loc[(t == 1) & (ts >= stop[0]) & (ts < stop[1]), "power_kw"] = 0.0
    status = empty_status()
    status.loc[0] = [1, stop[0], stop[1], "Stop", 1, "Frequency converter error", "Forced outage"]
    df, curves = prepare(farm, status, _static(6), BASELINE)
    return df, curves, stop


def test_reference_curves_cover_baseline(scenario):
    df, curves, _ = scenario
    assert set(curves) == {1, 2, 3, 4, 5, 6}
    c = curves[2]
    assert c["power_kw"].is_monotonic_increasing or c["power_kw"].diff().min() > -5


def test_neighbour_index_finds_the_real_fault_only(scenario):
    df, _, _ = scenario
    m = pf.performance_index(df, "M")["index_nb"].unstack("turbine")
    assert m.loc["2017-01"].to_numpy() == pytest.approx(1.0, abs=0.01)
    assert m.loc["2017-02", 3] == pytest.approx(0.9, abs=0.01)
    assert m.loc["2017-02", 5] < 0.9  # less wind -> less power than the neighbours
    assert m.loc["2017-02", [1, 2, 4, 6]].to_numpy() == pytest.approx(1.0, abs=0.01)


def test_power_curve_index_also_reacts_to_anemometer_drift(scenario):
    df, _, _ = scenario
    m = pf.performance_index(df, "M")["index_curve"].unstack("turbine")
    assert m.loc["2017-02", 3] == pytest.approx(0.9, abs=0.01)
    # anemometer reading 5 % low makes a healthy turbine look ~15 % better than its curve
    assert m.loc["2017-02", 4] > 1.08
    assert m.loc["2017-02", 5] == pytest.approx(1.0, abs=0.01)  # the turbine itself is fine


def test_underperformance_episode_and_trend(scenario):
    df, _, _ = scenario
    monthly = pf.performance_index(df, "M")
    episodes = pf.flag_underperformance(monthly, threshold=0.97, min_records=100)
    assert episodes["turbine"].tolist() == [3, 5]
    assert episodes.iloc[0][["start", "end", "months"]].tolist() == ["2017-02", "2017-03", 2]
    # T3 really converts less wind into power; T5 only sees less wind
    assert episodes["confirmed_by_power_curve"].tolist() == [True, False]
    trend = pf.index_trend(monthly, "index_nb", min_records=100)
    assert trend.loc[3, "slope_pct_per_year"] < -10


def test_lost_energy_during_stop_matches_neighbours(scenario):
    df, _, stop = scenario
    losses = pf.lost_energy(df)
    ts = df.index.get_level_values("timestamp")
    day = (ts >= stop[0]) & (ts < stop[1])
    neighbour_energy = df.loc[day & (df.index.get_level_values("turbine") == 2), "power_kw"].sum() / 6000
    assert losses.loc[(1, 2017), "lost_downtime_mwh"] == pytest.approx(neighbour_energy, rel=0.02)
    assert losses.loc[(2, 2017), "lost_downtime_mwh"] == 0
    assert losses.loc[(3, 2017), "performance_gap_mwh"] > 0


def test_yaw_offset_recovered():
    rng = np.random.default_rng(3)
    farm = make_farm(n_turbines=5, days=60)
    farm["vane"] = rng.normal(0, 6, len(farm))
    t2 = farm.index.get_level_values("turbine") == 2
    farm.loc[t2, "power_kw"] *= np.cos(np.deg2rad(farm.loc[t2, "vane"] - 6.0)) ** 2
    df, _ = prepare(farm, empty_status(), _static(5), (START, START + pd.Timedelta(days=60)))
    yaw = pf.yaw_misalignment(df, min_records=500, n_boot=20)
    row = yaw.loc[(2, 2017)]
    assert row["offset_deg"] == pytest.approx(6.0, abs=0.5)
    assert row["p05"] <= row["offset_deg"] <= row["p95"] and row["p95"] - row["p05"] < 2.0
    assert row["loss_pct"] == pytest.approx(100 * np.sin(np.deg2rad(6.0)) ** 2, abs=0.2)  # 1 - cos^2(6 deg)
