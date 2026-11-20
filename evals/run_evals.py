"""Run the eval scenarios against the real model and print a results table.

    python evals/run_evals.py --dry-run                 # list scenarios, no API calls
    python evals/run_evals.py                           # Haiku, 1 trial each (~1-2 cents)
    python evals/run_evals.py --model claude-sonnet-5-5 --trials 3

This costs money (model calls only; the road data is the committed snapshot). Each run is
saved under evals/results/ so you can compare prompts, models and effort levels over time.
"""
from __future__ import annotations

import argparse
import os
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from roadroute import RoadNetwork, RoutePlanner, Toolbox  # noqa: E402
from erfaert import Agent, AgentConfig  # noqa: E402
from erfaert.config import DEFAULT_MODEL  # noqa: E402
from roadroute.forecast import RouteForecast  # noqa: E402
from roadroute.weather import RouteWeather  # noqa: E402
from scenarios import (NOW, SCENARIOS, load_base_forecasts, load_base_observations,  # noqa: E402
                       load_base_snapshot, load_stations)

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--trials", type=int, default=1, help="runs per scenario (models are not deterministic)")
    parser.add_argument("--only", nargs="*", help="scenario ids or tags to run")
    parser.add_argument("--network", default=str(ROOT / "data" / "network.json.gz"))
    parser.add_argument("--no-weather", action="store_true", help="run without the weather tool")
    parser.add_argument("--dry-run", action="store_true", help="list the scenarios and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    scenarios = [s for s in SCENARIOS if not args.only or s.id in args.only or set(s.tags) & set(args.only)]
    if args.dry_run:
        for s in scenarios:
            print(f"{s.id:28} [{', '.join(s.tags)}] {s.question}")
            print(f"{'':28} checks: {', '.join(c.name for c in s.checks)}")
        print(f"\n{len(scenarios)} scenarios x {args.trials} trials = {len(scenarios) * args.trials} agent runs")
        return 0

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("No API key in this shell. PowerShell: $env:ANTHROPIC_API_KEY = 'sk-ant-...'  (this window only)\n"
              "or: setx ANTHROPIC_API_KEY sk-ant-...  (permanent, new windows only)", file=sys.stderr)
        return 2

    import anthropic

    client = anthropic.Anthropic()
    network = RoadNetwork.load(args.network)
    base = load_base_snapshot()
    stations, base_weather, base_forecasts = load_stations(), load_base_observations(), load_base_forecasts()
    config = AgentConfig(model=args.model, effort=args.effort)

    records = []
    for scenario in scenarios:
        snapshot = scenario.conditions(base)
        weather = forecast = None
        if not args.no_weather:
            observations = scenario.weather(base_weather)
            weather = RouteWeather(lambda: stations, lambda o=observations: o, clock=lambda: NOW)
            fcs = scenario.forecasts(base_forecasts)
            forecast = RouteForecast(lambda: stations, lambda ids, f=fcs: {i: f[i] for i in ids if i in f},
                                     clock=lambda: NOW)
        toolbox = Toolbox(RoutePlanner(network, lambda s=snapshot: s), weather=weather, forecast=forecast)
        agent = Agent(client, toolbox, config, clock=lambda: NOW)
        for trial in range(1, args.trials + 1):
            try:
                result = agent.run(scenario.question)
            except anthropic.APIError as exc:
                print(f"{scenario.id} trial {trial}: API error: {exc}", file=sys.stderr)
                return 1
            checks = {c.name: c(result) for c in scenario.checks}
            passed = all(ok for ok, _ in checks.values())
            mark = "PASS" if passed else "FAIL"
            print(f"{mark}  {scenario.id} #{trial}  ({result.usage.cost_usd * 100:.2f}¢, {result.iterations} calls)")
            for name, (ok, detail) in checks.items():
                if not ok:
                    print(f"      x {name}: {detail}")
            records.append({
                "scenario": scenario.id,
                "trial": trial,
                "passed": passed,
                "checks": {name: {"ok": ok, "detail": detail} for name, (ok, detail) in checks.items()},
                "run": result.to_dict(),
            })

    # Summary: pass rate per check across all runs, plus totals.
    per_check: dict[str, list[bool]] = defaultdict(list)
    for rec in records:
        for name, check in rec["checks"].items():
            per_check[name].append(check["ok"])
    total_cost = sum(r["run"]["usage"]["cost_usd"] for r in records)
    runs = len(records)
    print(f"\n{'check':40} pass rate")
    for name, oks in sorted(per_check.items()):
        print(f"{name:40} {sum(oks)}/{len(oks)}")
    passed_runs = sum(r["passed"] for r in records)
    avg_in = sum(r["run"]["usage"]["input_tokens"] + r["run"]["usage"]["cache_read_input_tokens"]
                 + r["run"]["usage"]["cache_creation_input_tokens"] for r in records) / runs
    avg_out = sum(r["run"]["usage"]["output_tokens"] for r in records) / runs
    print(f"\nruns passed: {passed_runs}/{runs}  |  model: {args.model} effort={args.effort}")
    print(f"avg tokens/run: {avg_in:,.0f} in, {avg_out:,.0f} out  |  total cost ~${total_cost:.4f}")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "-noweather" if args.no_weather else ""
    out = RESULTS_DIR / f"{stamp}-{args.model}-{args.effort}{suffix}.json"
    out.write_text(json.dumps({
        "model": args.model, "effort": args.effort, "trials": args.trials,
        "passed_runs": passed_runs, "runs": runs, "cost_usd": round(total_cost, 6),
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {out.relative_to(ROOT)}")
    return 0 if passed_runs == runs else 1


if __name__ == "__main__":
    sys.exit(main())
