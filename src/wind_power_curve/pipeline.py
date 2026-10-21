"""From raw SCADA + status records to an analysis-ready frame.

Steps: a consistent wind speed signal, classify records (cleaning), air density and normalised wind speed,
baseline reference curves, and the three expected-power estimates (own curve, neighbours' power, neighbours'
wind speed).

Usage:
    df, curves = prepare(scada, status, static, baseline=(pd.Timestamp("2017-01-01", tz="UTC"),
                                                          pd.Timestamp("2018-01-01", tz="UTC")))
"""
from __future__ import annotations

import pandas as pd

from wind_power_curve.cleaning import bin_outliers, classify, consistent_wind_speed
from wind_power_curve.performance import (expected_from_curves, neighbour_power, neighbour_wind_speed,
                                          reference_curves)
from wind_power_curve.power_curve import air_density, bin_power_curve, normalise_wind_speed


def prepare(scada: pd.DataFrame, status: pd.DataFrame, static: pd.DataFrame, baseline) -> tuple[pd.DataFrame, dict]:
    df = scada.copy()
    df["ws_scada"] = df["ws"]
    df["ws"] = consistent_wind_speed(df)
    df["state"] = classify(df, status, cod=static["cod"].min())["state"]

    # density from nacelle temperature and standard-atmosphere pressure at hub height
    turbine = df.index.get_level_values("turbine")
    height = (static["elevation_m"] + static["hub_height_m"]).reindex(turbine).to_numpy()
    rho = pd.Series(air_density(df["temp_c"], elevation_m=height), index=df.index)
    df["rho"] = rho.fillna(rho.groupby(level="turbine").transform("median"))
    df["ws_n"] = normalise_wind_speed(df["ws"], df["rho"])

    curves = reference_curves(df, baseline)
    df["p_curve"] = expected_from_curves(df, curves)
    df["p_ratio"] = neighbour_power(df, baseline)
    df["ws_nb"] = neighbour_wind_speed(df, baseline)
    df["p_nb"] = expected_from_curves(df, curves, ws_col="ws_nb")
    return df, curves


def yearly_curves(df: pd.DataFrame) -> pd.DataFrame:
    """Binned power curve per turbine and calendar year (normal operation, bin outliers removed)."""
    out = []
    sel = df[df["state"].eq("normal")].dropna(subset=["ws_n", "power_kw"])
    years = sel.index.get_level_values("timestamp").year
    for (turbine, year), d in sel.groupby([sel.index.get_level_values("turbine"), years]):
        keep = ~bin_outliers(d["ws_n"], d["power_kw"])
        c = bin_power_curve(d["ws_n"][keep], d["power_kw"][keep])
        out.append(c.assign(turbine=turbine, year=year).set_index(["turbine", "year"], append=True))
    return pd.concat(out).reorder_levels(["turbine", "year", "bin"]).sort_index()


def state_shares(df: pd.DataFrame) -> pd.DataFrame:
    """Share of 10-minute records in each state per turbine and year (after COD)."""
    d = df[df["state"].ne("pre_cod")]
    keys = [d.index.get_level_values("turbine"), d.index.get_level_values("timestamp").year]
    shares = pd.crosstab(keys, d["state"], normalize="index")
    shares.index.names = ["turbine", "year"]
    return shares.drop(columns=[c for c in shares.columns if (shares[c] == 0).all()])
