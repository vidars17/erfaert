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

### 5. Weather forecasts: `xmlweather.vedur.is`

- **Licensor:** Veðurstofa Íslands
- **Dataset:** station forecasts (`type=forec`, hourly where available) for temperature, mean wind, wind direction, weather and precipitation
- **Downloaded:** 2026-11-20 (forecast issued 12:00 UTC that day)
- **In this repository:** `tests/data/vedur_forecasts.snapshot.xml`, one downloaded response for the stations along the eval routes, unchanged apart from its name. At runtime, forecasts are downloaded for the stations along the requested route and cached for 30 minutes.
- **Terms:** the Met Office's [terms and conditions](https://en.vedur.is/about-imo/the-web/conditions) allow free use of its data, forecasts included, provided the Met Office and the download date are cited and any changes are stated clearly.

## Changes made to the data

CC BY 4.0 requires saying whether the data was changed. It was:

- **`data/network.json.gz`** is a routing graph built from dataset 2. Duplicate copies of the same road stretch (stored at several levels of detail) were reduced to the most detailed one, line endpoints within 10 m were joined, and the result was saved as a graph of nodes and segments. Segment ids and names are unchanged. See `src/roadroute/network.py` and `src/roadroute/geometry.py`.
- **Tool and agent output** summarises dataset 1 for a route. It selects and condenses records but does not change their values.
- **Eval scenarios (`evals/scenarios.py`) deliberately change the conditions snapshot in memory** to create test cases: fictional closures, a fictional wind warning, ice and height limit, and a fake injected instruction. **These changes are invented test data, not Vegagerðin data.** Saved eval transcripts in `evals/results/` contain this altered data and must not be read as real road conditions.
- **Weather tool output** selects the stations near a route and summarises their observations (ice and gust flags use this project's thresholds). It does not change the observed values.
- **Eval scenarios also change the weather snapshot in memory:** every observation time is moved two weeks back, so the newest is 5 minutes before the eval clock (2026-11-06 15:00 UTC), to match the road snapshot. One scenario replaces the Holtavörðuheiði readings with invented storm values (33 m/s gusts). **These are invented test data, not Veðurstofa Íslands data.**
- **Forecast tool output** gives each station's forecast for the estimated arrival time (an assumed 70 km/h) and adds wind, ice and snow flags using this project's thresholds. It does not change the forecast values.
- **Eval scenarios change the forecast snapshot in memory:** all times are moved exactly 14 days back (issued 2026-11-06 12:00), and one scenario replaces Holtavörðuheiði's forecast on the morning of 2026-11-09 with an invented storm (mean wind 24 m/s, snow, -3 °C). **These are invented test data, not Veðurstofa Íslands forecasts.**

## How to cite

> Road condition data and road segment geometry: Vegagerðin, datasets `faerd2017_1` (downloaded 2026-11-06) and `faerdferlar2017_1` (downloaded November 2026), licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Weather observations and station data: Veðurstofa Íslands, api.vedur.is (downloaded 2026-11-20), licensed under CC BY 4.0. Weather forecasts: Veðurstofa Íslands, xmlweather.vedur.is (downloaded 2026-11-20). Modified: see DATA_LICENSE.md.
