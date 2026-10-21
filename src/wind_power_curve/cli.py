"""Command line interface.

Usage:
    python -m wind_power_curve fetch --years 2016 2017 2018 2019 2020 2021
    python -m wind_power_curve run   --years 2016 2017 2018 2019 2020 2021 --baseline-year 2017
    python -m wind_power_curve run   --help

Data are cached in ./data (override with --data-dir or KELMARSH_DATA_DIR), results go to ./results and charts
to ./docs/img.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import pandas as pd

from wind_power_curve import plots
from wind_power_curve.performance import (expected_from_curves, flag_underperformance, index_trend, lost_energy,
                                          performance_index, reference_curves, yaw_misalignment)
from wind_power_curve.pipeline import prepare, state_shares, yearly_curves
from wind_power_curve.power_curve import MM92_CURVE, RATED_KW, aep_rayleigh, database_complete, normalise_wind_speed
from wind_power_curve.scada import load, read_static
from wind_power_curve.zenodo import download

UNDERPERFORMANCE_THRESHOLD = 0.97
log = logging.getLogger("wind_power_curve")


def cmd_fetch(args):
    for path in download(args.years, args.data_dir):
        log.info("ok %s", path)


def aep_table(curves: pd.DataFrame, mean_ws=(6.0, 7.0, 8.0)) -> pd.DataFrame:
    """AEP of each turbine-year curve relative to the catalogue curve, for Rayleigh mean wind speeds."""
    rows = []
    for (turbine, year), c in curves.groupby(level=["turbine", "year"]):
        c = c.droplevel(["turbine", "year"])
        row = {"turbine": turbine, "year": year, "complete": database_complete(c, RATED_KW), "hours": c["n"].sum() / 6}
        for v in mean_ws:
            ref = aep_rayleigh(MM92_CURVE.index, MM92_CURVE.values, v)
            row[f"aep_ratio_{v:g}ms"] = aep_rayleigh(c["ws"], c["power_kw"], v) / ref
        rows.append(row)
    return pd.DataFrame(rows).set_index(["turbine", "year"])


def cmd_run(args):
    years = sorted(args.years)
    if args.baseline_year not in years:
        raise SystemExit("--baseline-year must be one of --years")
    scada, status = load(years, args.data_dir, refresh=args.refresh)
    static = read_static(args.data_dir)
    baseline = (pd.Timestamp(f"{args.baseline_year}-01-01", tz="UTC"), pd.Timestamp(f"{args.baseline_year + 1}-01-01", tz="UTC"))
    df, curves = prepare(scada, status, static, baseline)
    log.info("%d records, states: %s", len(df), df["state"].value_counts().to_dict())

    res = args.results_dir
    res.mkdir(parents=True, exist_ok=True)
    shares = state_shares(df)
    shares.to_csv(res / "data_states.csv", float_format="%.4f")
    ref_table = pd.concat({t: c for t, c in curves.items()}, names=["turbine"])
    ref_table.to_csv(res / f"reference_curves_{args.baseline_year}.csv", float_format="%.2f")
    by_year = yearly_curves(df)
    by_year.to_csv(res / "power_curves_by_year.csv", float_format="%.2f")
    aep = aep_table(by_year)
    aep.to_csv(res / "aep_vs_catalogue.csv", float_format="%.4f")

    monthly = performance_index(df, "M")
    monthly.to_csv(res / "monthly_index.csv", float_format="%.4f")
    annual = performance_index(df, "Y")
    # the same power curve index computed with the raw SCADA wind speed, to show what the signal change does
    raw = df.assign(ws_n=normalise_wind_speed(df["ws_scada"], df["rho"]))
    raw["p_curve"] = expected_from_curves(raw, reference_curves(raw, baseline))
    annual["index_curve_scada_ws"] = performance_index(raw, "Y")["index_curve"]
    annual.to_csv(res / "annual_index.csv", float_format="%.4f")
    trend = index_trend(monthly, "index_nb")
    trend.to_csv(res / "trend_index_nb.csv", float_format="%.3f")
    episodes = flag_underperformance(monthly, UNDERPERFORMANCE_THRESHOLD)
    episodes.to_csv(res / "underperformance_episodes.csv", index=False, float_format="%.4f")
    yaw = yaw_misalignment(df)
    yaw.to_csv(res / "yaw_misalignment.csv", float_format="%.3f")
    losses = lost_energy(df)
    losses.to_csv(res / "lost_energy.csv", float_format="%.2f")

    img = args.img_dir
    example_year = years[-1]
    plots.cleaning_example(df, 1, example_year, curves[1], img / "cleaning_example.png")
    plots.power_curves(curves, args.baseline_year, img / "power_curves.png")
    plots.performance_index(monthly, UNDERPERFORMANCE_THRESHOLD, img / "performance_index.png")
    plots.lost_energy(losses, img / "lost_energy.png")
    plots.wind_speed_signal(df, img / "wind_speed_signal.png")
    pooled = yaw[yaw.index.get_level_values("year").astype(str) == "all"]
    if len(pooled):
        example = (pooled["p95"] - pooled["p05"]).idxmin()[0]  # the best-determined turbine
        plots.yaw_misalignment(df, yaw, example, img / "yaw_misalignment.png")

    summary = {
        "years": years, "baseline_year": args.baseline_year, "records": int(len(df)),
        "states": {k: int(v) for k, v in df["state"].value_counts().items()},
        "trend_index_nb_pct_per_year": trend["slope_pct_per_year"].round(3).to_dict(),
        "underperformance_episodes": len(episodes),
        "lost_energy_mwh": losses.drop(columns=["energy_availability"]).sum().round(1).to_dict(),
    }
    (res / "run.json").write_text(json.dumps(summary, indent=2, default=str))
    log.info("results in %s, charts in %s", res, img)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wind-power-curve", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "run"):
        p = sub.add_parser(name)
        p.add_argument("--years", type=int, nargs="+", default=list(range(2016, 2022)))
        p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("KELMARSH_DATA_DIR", "data")))
        if name == "run":
            p.add_argument("--baseline-year", type=int, default=2017, help="year whose behaviour is the reference")
            p.add_argument("--refresh", action="store_true", help="re-read the archives instead of the Parquet cache")
            p.add_argument("--results-dir", type=Path, default=Path("results"))
            p.add_argument("--img-dir", type=Path, default=Path("docs/img"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    {"fetch": cmd_fetch, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
