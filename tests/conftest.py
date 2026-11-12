from __future__ import annotations

from pathlib import Path

import pytest

from builders import make_pieces
from roadroute.network import RoadNetwork

DATA_DIR = Path(__file__).parent / "data"
CONDITIONS_SNAPSHOT = DATA_DIR / "faerd2017_1.snapshot.xml"


@pytest.fixture()
def network() -> RoadNetwork:
    return RoadNetwork(make_pieces(), snap_m=3.0)
