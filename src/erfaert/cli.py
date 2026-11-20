"""erfaert "Can I drive from Reykjavík to Akureyri?" """
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from roadroute import CachedConditions, RoadNetwork, RoutePlanner, Toolbox, fetch_conditions, load_conditions
from roadroute.conditions import ATTRIBUTION
from roadroute.weather import ATTRIBUTION as WEATHER_ATTRIBUTION
from roadroute.weather import Cached, RouteWeather, fetch_latest, fetch_stations

from .config import DEFAULT_MODEL, AgentConfig
from .loop import Agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="erfaert", description=__doc__)
    parser.add_argument("question")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--max-iterations", type=int, default=6)
    parser.add_argument("--network", default="data/network.json.gz")
    parser.add_argument(
        "--conditions",
        default="live",
        help="'live' to fetch Vegagerðin's feed, or a path to a saved faerd2017_1 XML file",
    )
    parser.add_argument("--no-weather", action="store_true",
                        help="don't offer the weather tool (Veðurstofa Íslands observations)")
    parser.add_argument("--json", action="store_true", help="print the full run record as JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every loop iteration")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        import anthropic
    except ImportError:
        print("The agent needs the Anthropic SDK: pip install -e \".[agent]\"", file=sys.stderr)
        return 2

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("No API key in this shell. PowerShell: $env:ANTHROPIC_API_KEY = 'sk-ant-...'  (this window only)\n"
              "or: setx ANTHROPIC_API_KEY sk-ant-...  (permanent, new windows only)", file=sys.stderr)
        return 2

    if args.conditions == "live":
        source = CachedConditions(fetch_conditions)
    else:
        snapshot = load_conditions(args.conditions)
        source = lambda: snapshot  # noqa: E731 - a fixed snapshot
    weather = None
    if not args.no_weather:
        weather = RouteWeather(Cached(fetch_stations, ttl_s=24 * 3600), Cached(fetch_latest, ttl_s=300))
    toolbox = Toolbox(RoutePlanner(RoadNetwork.load(args.network), source), weather=weather)
    config = AgentConfig(model=args.model, effort=args.effort, max_iterations=args.max_iterations)
    agent = Agent(anthropic.Anthropic(), toolbox, config)

    try:
        result = agent.run(args.question)
    except anthropic.AuthenticationError:
        print("No valid API credentials. Set ANTHROPIC_API_KEY (see README).", file=sys.stderr)
        return 2
    except anthropic.APIError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.answer or f"(no answer: run ended with {result.stop})")
        tools = ", ".join(f"{c.name}{' [error]' if c.is_error else ''}" for c in result.tool_calls)
        print(f"\n--- {result.stop} after {result.iterations} model calls | tools: {tools or 'none'}")
        print(f"--- {result.usage.summary()} | {result.duration_s:.1f}s | {', '.join(result.models_used)}")
        print(f"--- {ATTRIBUTION}")
        if any(c.name == "get_route_weather" for c in result.tool_calls):
            print(f"--- {WEATHER_ATTRIBUTION}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
