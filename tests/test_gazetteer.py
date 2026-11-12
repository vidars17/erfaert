import pytest

from roadroute.gazetteer import PLACES, Gazetteer, UnknownPlaceError, normalize

gazetteer = Gazetteer()


@pytest.mark.parametrize(
    "text, expected",
    [("Ísafjörður", "isafjordur"), ("Þingvellir", "thingvellir"), ("  VÍK í  Mýrdal ", "vik i myrdal"),
     ("Æðey", "aedey"), ("Reykjavik!", "reykjavik")],
)
def test_normalize(text, expected):
    assert normalize(text) == expected


@pytest.mark.parametrize(
    "query, canonical",
    [("Reykjavik", "Reykjavík"), ("isafjordur", "Ísafjörður"), ("THINGVELLIR", "Þingvellir"),
     ("Vik", "Vík í Mýrdal"), ("KEF", "Keflavík Airport"), ("Klaustur", "Kirkjubæjarklaustur"),
     ("hofn", "Höfn")],
)
def test_resolves_spelling_variants_and_aliases(query, canonical):
    assert gazetteer.resolve(query).name == canonical


def test_coordinates_are_accepted():
    place = gazetteer.resolve("64.1466, -21.9426")
    assert (place.lat, place.lon) == (64.1466, -21.9426)


@pytest.mark.parametrize("query", ["48.85,2.35", "0,0", "200,-21"])
def test_coordinates_outside_iceland_are_rejected(query):
    with pytest.raises(UnknownPlaceError):
        gazetteer.resolve(query)


def test_typos_get_suggestions():
    with pytest.raises(UnknownPlaceError) as err:
        gazetteer.resolve("Akureyi")
    assert "Akureyri" in err.value.suggestions
    assert "Did you mean" in str(err.value)


def test_unknown_place_without_close_match():
    with pytest.raises(UnknownPlaceError) as err:
        gazetteer.resolve("Atlantis")
    assert err.value.suggestions == []


def test_no_two_places_collapse_to_the_same_key():
    keys = [normalize(name) for name in PLACES]
    assert len(keys) == len(set(keys))


def test_all_coordinates_are_inside_iceland():
    for name, (lat, lon) in PLACES.items():
        assert 63.0 < lat < 67.0 and -25.0 < lon < -13.0, name
