import numpy as np
import pandas as pd
import pytest

from conftest import START, empty_status, make_farm
from wind_power_curve import cleaning


def _ts(*minutes):
    return pd.DatetimeIndex([START + pd.Timedelta(minutes=m) for m in minutes])


def test_event_mask_overlap_rules():
    ts = pd.date_range(START, periods=6, freq="10min")
    # event 00:15-00:31 overlaps the intervals starting 00:10, 00:20 and 00:30
    mask = cleaning.event_mask(ts, _ts(15), _ts(31))
    assert mask.tolist() == [False, True, True, True, False, False]
    # an event ending exactly at 00:20 does not touch the 00:20 interval
    assert cleaning.event_mask(ts, _ts(0), _ts(20)).tolist() == [True, True, False, False, False, False]
    # an open event (no end) counts as one interval
    open_end = cleaning.event_mask(ts, _ts(40), pd.DatetimeIndex([pd.NaT], tz="UTC"))
    assert open_end.tolist() == [False, False, False, False, True, False]


def test_frozen_signal():
    s = pd.Series([1.0, 2.0, 2.0, 2.0, 3.0, 3.0, np.nan, np.nan, np.nan])
    assert cleaning.frozen(s, n=3).tolist() == [False, True, True, True, False, False, False, False, False]


def test_classify_states():
    farm = make_farm(n_turbines=3, days=30)
    ts = farm.loc[1].index
    windy = ts[farm.loc[1, "ws"].to_numpy() > 8]
    status = empty_status()
    status.loc[0] = [1, windy[100], windy[100] + pd.Timedelta("1h"), "Stop", 1, "Manual stop", "Scheduled Maintenance"]
    status.loc[1] = [2, windy[300], windy[300] + pd.Timedelta("30min"), "Stop", 2, "Grid loss",
                     "Out of Electrical Specification"]
    farm.loc[(1, windy[100]):(1, windy[100] + pd.Timedelta("50min")), "power_kw"] = 0.0
    farm.loc[(3, windy[500]), ["pitch", "power_kw"]] = [15.0, 400.0]  # pitched out, derated
    farm.loc[(3, windy[501]), "power_kw"] = np.nan

    flags = cleaning.classify(farm, status, cod=ts[10])
    state = flags["state"]
    assert (state.loc[1].iloc[:10] == "pre_cod").all()
    assert state.loc[(1, windy[100])] == "stop"
    assert state.loc[(2, windy[300])] == "stop_grid"
    assert state.loc[(3, windy[500])] == "curtailed"
    assert state.loc[(3, windy[501])] == "missing"
    assert (state == "normal").mean() > 0.95


def test_zero_power_in_good_wind_is_a_stop_even_without_status():
    farm = make_farm(n_turbines=2, days=10)
    windy = farm.loc[2].index[farm.loc[2, "ws"].to_numpy() > 9][50]
    farm.loc[(2, windy), "power_kw"] = 0.0
    state = cleaning.classify(farm, empty_status(), cod=START)["state"]
    assert state.loc[(2, windy)] == "stop"


def test_sensor_disagreement_and_icing():
    farm = make_farm(n_turbines=2, days=20)
    idx = farm.loc[1].index
    good = idx[(farm.loc[1, "ws"] > 8).to_numpy()]
    farm.loc[(1, good[10]), ["ws1", "ws2"]] = [9.0, 6.0]  # anemometers disagree by 3 m/s
    farm.loc[(1, good[20]), ["temp_c", "power_kw"]] = [-1.0, 50.0]  # cold and far below the curve
    state = cleaning.classify(farm, empty_status(), cod=START)["state"]
    assert state.loc[(1, good[10])] == "sensor"
    assert state.loc[(1, good[20])] == "icing"


def test_bin_outliers_keeps_the_bulk():
    rng = np.random.default_rng(1)
    ws = rng.uniform(4, 10, 5000)
    p = 20 * ws ** 3 + rng.normal(0, 20, ws.size)
    p[:10] = 0.0  # clear outliers
    out = cleaning.bin_outliers(ws, p)
    assert out[:10].all()
    assert out[10:].mean() < 0.01


def test_consistent_wind_speed_uses_mean_of_anemometers():
    df = pd.DataFrame({"ws": [7.0, 6.0, 5.0], "ws1": [7.2, np.nan, 5.4], "ws2": [6.8, 6.1, 4.8]})
    assert cleaning.consistent_wind_speed(df).tolist() == pytest.approx([7.0, 6.0, 5.1])
