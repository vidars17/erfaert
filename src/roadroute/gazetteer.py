"""A small hand-made gazetteer: place name -> (lat, lon).

Coordinates are approximate town centres (good to roughly a kilometre); the
router snaps them to the nearest road, and the integration test checks that
every entry lands close to a road in the real network. Matching ignores case,
accents and the Icelandic letters ð/þ/æ, so "Isafjordur" finds "Ísafjörður".
Coordinates such as "64.1466,-21.9426" are accepted as well.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

_LETTERS = str.maketrans({"ð": "d", "þ": "th", "æ": "ae"})


def normalize(text: str) -> str:
    text = text.strip().casefold().translate(_LETTERS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float


class UnknownPlaceError(ValueError):
    def __init__(self, query: str, suggestions: list[str]) -> None:
        self.query, self.suggestions = query, suggestions
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(f"Unknown place {query!r}.{hint}")


PLACES: dict[str, tuple[float, float]] = {
    # Capital region
    "Reykjavík": (64.1466, -21.9426),
    "Kópavogur": (64.1100, -21.9100),
    "Hafnarfjörður": (64.0671, -21.9377),
    "Garðabær": (64.0886, -21.9230),
    "Mosfellsbær": (64.1667, -21.6933),
    # Reykjanes
    "Keflavík": (64.0010, -22.5610),
    "Keflavík Airport": (63.9850, -22.6056),
    "Grindavík": (63.8424, -22.4338),
    # South
    "Selfoss": (63.9331, -20.9971),
    "Hveragerði": (64.0000, -21.1860),
    "Þorlákshöfn": (63.8564, -21.3790),
    "Hella": (63.8330, -20.3830),
    "Hvolsvöllur": (63.7530, -20.2240),
    "Skógar": (63.5260, -19.5110),
    "Vík í Mýrdal": (63.4186, -19.0060),
    "Kirkjubæjarklaustur": (63.7900, -18.0540),
    "Skaftafell": (64.0160, -16.9690),
    "Jökulsárlón": (64.0484, -16.1795),
    "Höfn": (64.2539, -15.2082),
    "Flúðir": (64.1370, -20.3140),
    "Laugarvatn": (64.2130, -20.7310),
    "Þingvellir": (64.2559, -21.1299),
    "Geysir": (64.3104, -20.3024),
    "Gullfoss": (64.3271, -20.1199),
    # West
    "Akranes": (64.3217, -22.0746),
    "Borgarnes": (64.5380, -21.9200),
    "Stykkishólmur": (65.0750, -22.7300),
    "Grundarfjörður": (64.9230, -23.2550),
    "Ólafsvík": (64.8940, -23.7110),
    "Búðardalur": (65.1110, -21.7710),
    # Westfjords
    "Ísafjörður": (66.0749, -23.1350),
    "Bolungarvík": (66.1550, -23.2500),
    "Súðavík": (66.0390, -22.9930),
    "Flateyri": (66.0500, -23.5170),
    "Þingeyri": (65.8720, -23.4830),
    "Patreksfjörður": (65.5960, -23.9780),
    "Bíldudalur": (65.6850, -23.5970),
    "Reykhólar": (65.4500, -22.2060),
    "Hólmavík": (65.7060, -21.6800),
    # North
    "Hvammstangi": (65.3960, -20.9420),
    "Blönduós": (65.6600, -20.2800),
    "Sauðárkrókur": (65.7460, -19.6390),
    "Varmahlíð": (65.5540, -19.4380),
    "Siglufjörður": (66.1530, -18.9090),
    "Ólafsfjörður": (66.0730, -18.6520),
    "Dalvík": (65.9700, -18.5290),
    "Akureyri": (65.6835, -18.1005),
    "Húsavík": (66.0440, -17.3390),
    "Mývatn": (65.6430, -16.9150),
    "Ásbyrgi": (66.0170, -16.5100),
    "Kópasker": (66.2830, -16.3470),
    "Raufarhöfn": (66.4590, -15.9440),
    "Þórshöfn": (66.2000, -15.3330),
    "Vopnafjörður": (65.7570, -14.8350),
    # East
    "Egilsstaðir": (65.2669, -14.3948),
    "Seyðisfjörður": (65.2590, -14.0080),
    "Neskaupstaður": (65.1470, -13.6840),
    "Eskifjörður": (65.0750, -14.0150),
    "Reyðarfjörður": (65.0320, -14.2190),
    "Fáskrúðsfjörður": (64.9290, -14.0150),
    "Stöðvarfjörður": (64.8280, -13.8740),
    "Breiðdalsvík": (64.7900, -14.0000),
    "Djúpivogur": (64.6570, -14.2800),
    "Borgarfjörður eystri": (65.5200, -13.8100),
}

ALIASES: dict[str, str] = {
    "Reykjanesbær": "Keflavík",
    "KEF": "Keflavík Airport",
    "Keflavik International Airport": "Keflavík Airport",
    "Vik": "Vík í Mýrdal",
    "Myvatn": "Mývatn",
    "Reykjahlíð": "Mývatn",
    "Klaustur": "Kirkjubæjarklaustur",
    "Hofn": "Höfn",
    "Neskaupstadur": "Neskaupstaður",
}

# Rough bounding box of Iceland, used to reject nonsense coordinates.
_LAT_RANGE, _LON_RANGE = (62.9, 67.2), (-25.0, -12.9)
_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$")


class Gazetteer:
    def __init__(self, places: dict[str, tuple[float, float]] = PLACES,
                 aliases: dict[str, str] = ALIASES) -> None:
        self._places = places
        self._index = {normalize(name): name for name in places}
        for alias, target in aliases.items():
            self._index.setdefault(normalize(alias), target)

    @property
    def names(self) -> list[str]:
        return sorted(self._places)

    def resolve(self, query: str) -> Place:
        match = _COORDS.match(query)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            if not (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1] and _LON_RANGE[0] <= lon <= _LON_RANGE[1]):
                raise UnknownPlaceError(query, [])
            return Place(f"{lat:.4f},{lon:.4f}", lat, lon)
        key = normalize(query)
        name = self._index.get(key)
        if name is None:
            close = difflib.get_close_matches(key, self._index, n=3, cutoff=0.6)
            raise UnknownPlaceError(query, [self._index[c] for c in close])
        lat, lon = self._places[name]
        return Place(name, lat, lon)
