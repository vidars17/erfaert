# Data sources and attribution

This project uses open data from **Vegagerðin** (the Icelandic Road and Coastal Administration), licensed under the [Creative Commons Attribution 4.0 International licence (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/). Attribution follows Vegagerðin's terms of use and point 2, paragraph 1, article 4 of Act No. 45/2018.

It also uses weather observations from **Veðurstofa Íslands** (the Icelandic Met Office, IMO) via [api.vedur.is/weather](https://api.vedur.is/weather/), which the API publishes under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), with terms of service at [en.vedur.is/about-imo/the-web/conditions](https://en.vedur.is/about-imo/the-web/conditions). Some of the stations are owned by Vegagerðin, Landsvirkjun and others; IMO distributes their data through the API.

This project is not affiliated with Vegagerðin or Veðurstofa Íslands. Neither endorses it, has approved how it processes the data, or recommends any particular use of it. For official road information, see [umferdin.is](https://umferdin.is) or call 1777; for official weather and forecasts, see [vedur.is](https://vedur.is).

The source code is licensed separately (see `LICENSE`). This file covers the data only.

## Datasets

### 1. Road conditions: `faerd2017_1`

- **Licensor:** Vegagerðin
- **Dataset:** current road conditions per road segment (færð á vegum), from Vegagerðin's open data service at `http://gagnaveita.vegagerdin.is/api/faerd2017_1`
- **Downloaded:** 2026-11-06 (the feed itself records that it was generated at 13:11 UTC that day)
- **In this repository:** `tests/data/faerd2017_1.snapshot.xml`, the downloaded file, unchanged apart from its name. It is used by the tests and the evals.
- **At runtime:** with `--conditions live`, the feed is downloaded when the program runs. The time the data was generated is shown in every result (`data_as_of`).
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

### 2. Road segment geometry: `faerdferlar2017_1`

- **Licensor:** Vegagerðin
- **Dataset:** line geometry of the road-condition segments, layer `gis:faerdferlar2017_1` from Vegagerðin's GeoServer (WFS) at `http://gagnaveita.vegagerdin.is/geoserver`
- **Downloaded:** November 2026, on or before 2026-11-10
- **In this repository:** not included in raw form. `data/network.json.gz` is **derived** from it.
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

### 3. Weather stations: `/stations`

- **Licensor:** Veðurstofa Íslands
- **Dataset:** station metadata (id, name, coordinates, elevation, owner) from `https://api.vedur.is/weather/stations?active=true`
- **Downloaded:** 2026-11-20
- **In this repository:** `tests/data/vedur_stations.snapshot.json`, the downloaded file, unchanged apart from its name. At runtime the list is downloaded and cached for 24 hours.
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

### 4. Weather observations: `/observations/aws/10min/latest`

- **Licensor:** Veðurstofa Íslands (stations owned by IMO, Vegagerðin and others)
- **Dataset:** latest 10-minute observations from automatic weather stations, `parameters=basic`
- **Downloaded:** 2026-11-20 (observations from 14:10-14:20 UTC that day)
- **In this repository:** `tests/data/vedur_aws_10min_latest.snapshot.json`, the downloaded file, unchanged apart from its name. At runtime the observations are downloaded and cached for 5 minutes.
- **Licence:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

## Changes made to the data

CC BY 4.0 requires saying whether the data was changed. It was:

- **`data/network.json.gz`** is a routing graph built from dataset 2. Duplicate copies of the same road stretch (stored at several levels of detail) were reduced to the most detailed one, line endpoints within 10 m were joined, and the result was saved as a graph of nodes and segments. Segment ids and names are unchanged. See `src/roadroute/network.py` and `src/roadroute/geometry.py`.
- **Tool and agent output** summarises dataset 1 for a route. It selects and condenses records but does not change their values.
- **Eval scenarios (`evals/scenarios.py`) deliberately change the conditions snapshot in memory** to create test cases: fictional closures, a fictional wind warning, ice and height limit, and a fake injected instruction. **These changes are invented test data, not Vegagerðin data.** Saved eval transcripts in `evals/results/` contain this altered data and must not be read as real road conditions.
- **Weather tool output** selects the stations near a route and summarises their observations (ice and gust flags use this project's thresholds). It does not change the observed values.
- **Eval scenarios also change the weather snapshot in memory:** every observation time is moved two weeks back, so the newest is 5 minutes before the eval clock (2026-11-06 15:00 UTC), to match the road snapshot. One scenario replaces the Holtavörðuheiði readings with invented storm values (33 m/s gusts). **These are invented test data, not Veðurstofa Íslands data.**

## How to cite

> Road condition data and road segment geometry: Vegagerðin, datasets `faerd2017_1` (downloaded 2026-11-06) and `faerdferlar2017_1` (downloaded November 2026), licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Weather observations and station data: Veðurstofa Íslands, api.vedur.is (downloaded 2026-11-20), licensed under CC BY 4.0. Modified: see DATA_LICENSE.md.
