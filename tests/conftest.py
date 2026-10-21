"""Synthetic wind farm used by the tests: identical wind at every turbine, catalogue power curve, no noise."""
import numpy as np
import pandas as pd
import pytest

from wind_power_curve.power_curve import MM92_CURVE, interpolate
from wind_power_curve.scada import COLUMNS

START = pd.Timestamp("2017-01-01", tz="UTC")


def make_farm(n_turbines: int = 5, days: int = 60, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(START, periods=days * 144, freq="10min")
    ws = np.clip(7 + 3 * np.sin(np.arange(len(ts)) / 40.0) + rng.normal(0, 1.0, len(ts)), 0.5, 18)
    wind_dir = (200 + 60 * np.sin(np.arange(len(ts)) / 300.0)) % 360
    frames = []
    for t in range(1, n_turbines + 1):
        df = pd.DataFrame(index=ts.rename("timestamp"))
        for col in COLUMNS.values():
            df[col] = np.nan
        df["ws"] = ws
        df["power_kw"] = interpolate(MM92_CURVE.index, MM92_CURVE.values, ws)
        df["wind_dir"] = wind_dir
        df["temp_c"] = 10.0
        df["pitch"] = 0.0
        df.insert(0, "turbine", t)
        frames.append(df.set_index("turbine", append=True).swaplevel())
    return pd.concat(frames).sort_index()


def empty_status() -> pd.DataFrame:
    return pd.DataFrame({"turbine": pd.Series(dtype="int8"), "start": pd.Series(dtype="datetime64[ns, UTC]"),
                         "end": pd.Series(dtype="datetime64[ns, UTC]"), "status": pd.Series(dtype=str),
                         "code": pd.Series(dtype=float), "message": pd.Series(dtype=str),
                         "iec_category": pd.Series(dtype=str)})


@pytest.fixture
def farm():
    return make_farm()
