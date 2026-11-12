"""Command line: build the network file once, then query it."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .conditions import ATTRIBUTION, CachedConditions, fetch_conditions, load_conditions
from .gazetteer import Gazetteer
from .network import RoadNetwork
from .service import RoutePlanner
from .tools import Toolbox


def _build(args: argparse.Namespace) -> int:
    network = RoadNetwork.from_geometry_xml(args.geometry_xml, snap_m=args.snap_m)
    network.save(args.output)
    print(
        f"segments: {len(network.seg_ids)}  nodes: {len(network.nodes)}  "
        f"duplicate lines dropped: {network.dropped_duplicates}  snap joins: {network.join_count}\n"
        f"connected components: {len(network.component_sizes)}  "
        f"main network: {network.main_share:.1%} of nodes"
    )
    if network.disconnected_segments:
        print(f"segments not connected to the main network ({len(network.disconnected_segments)}):")
        for info in network.disconnected_segments:
            print(f"  {info.id}  {info.name}")
    print(f"saved: {args.output} ({Path(args.output).stat().st_size / 1e6:.1f} MB)")
    return 0


def _route(args: argparse.Namespace) -> int:
    network = RoadNetwork.load(args.network)
    if args.conditions == "live":
        source = CachedConditions(fetch_conditions)
    else:
        snapshot = load_conditions(args.conditions)
        source = lambda: snapshot  # noqa: E731 - a fixed snapshot
    toolbox = Toolbox(RoutePlanner(network, source))
    payload = {"origin": args.origin, "destination": args.destination, "vehicle": args.vehicle}
    print(json.dumps(json.loads(toolbox.call("get_route_conditions", payload)),
                     ensure_ascii=False, indent=2))
    print(ATTRIBUTION, file=sys.stderr)  # stderr, so stdout stays valid JSON
    return 0


def _places(_: argparse.Namespace) -> int:
    print("\n".join(Gazetteer().names))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roadroute", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="parse the geometry XML into a compact network file")
    build.add_argument("geometry_xml")
    build.add_argument("-o", "--output", default="data/network.json.gz")
    build.add_argument("--snap-m", type=float, default=10.0, help="endpoint snap distance in metres")
    build.set_defaults(func=_build)

    route = sub.add_parser("route", help="route between two places and print the tool result")
    route.add_argument("origin")
    route.add_argument("destination")
    route.add_argument("--network", default="data/network.json.gz")
    route.add_argument("--conditions", default="live",
                       help="'live' to fetch from Vegagerðin, or a path to a saved faerd2017_1.xml")
    route.add_argument("--vehicle", default="standard", choices=["standard", "4x4"])
    route.set_defaults(func=_route)

    places = sub.add_parser("places", help="list known place names")
    places.set_defaults(func=_places)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
