# roadroute

Condition-aware routing over Iceland's Vegagerðin road network, packaged as a tool for an LLM agent.

Given two places, it finds a route over the road network, **routes around segments that are closed or impassable right now**, and returns a compact summary of current conditions along the way (a few hundred tokens). The point of the design is that **the model never does geography**: code finds the route, the model interprets the result and writes the answer.

```
"Can I drive Reykjavík → Akureyri?"
        │
        ▼  model decides to call a tool
get_route_conditions(origin, destination)          ← this package
        │   gazetteer → snap to road → Dijkstra with condition-aware costs → join live conditions
        ▼
{ total_km, issues[], segments[[name, km, status]…], data_as_of, shorter_route_blocked? }
        │
        ▼  model explains, states the data timestamp, flags anything not GREIDFAERT
```

## Quick start

```bash
pip install -e ".[dev]"

# one-off: parse Vegagerðin's geometry export into a compact graph (~3 s, ~1 MB)
roadroute build path/to/faerdferlar2017_1.xml -o data/network.json.gz

# route using a saved conditions file, or --conditions live to fetch the feed
roadroute route Reykjavík Akureyri --conditions tests/data/faerd2017_1.snapshot.xml
roadroute route Reykjavík Egilsstaðir --vehicle 4x4 --conditions live
```

`data/network.json.gz` is already built and committed, so `roadroute route` works straight away.

As a library:

```python
from roadroute import RoadNetwork, RoutePlanner, Toolbox, CachedConditions, fetch_conditions, TOOL_DEFINITIONS

network = RoadNetwork.load("data/network.json.gz")
toolbox = Toolbox(RoutePlanner(network, CachedConditions(fetch_conditions, ttl_s=300)))

# TOOL_DEFINITIONS is in the Anthropic Messages API `tools` format. When the model returns a
# tool_use block, run it and send the string back as the tool_result content:
result_text = toolbox.call(block.name, block.input)   # never raises; errors come back as JSON
```

The agent loop that calls these tools is in a separate package, `roadroute_agent` (next section).

## The agent (`roadroute_agent`)

A hand-written tool-use loop over the Claude Messages API, with no framework and no SDK tool runner, so every step is visible: send the messages, run the tools the model asks for (concurrently when it asks for several), send all results back in one message, repeat. Guards: an iteration cap, `max_tokens` and `refusal` stop reasons reported rather than ignored, tool failures returned to the model as `is_error` results.

```bash
pip install -e ".[agent]"
export ANTHROPIC_API_KEY=...      # set a monthly spend limit in the Console first

roadroute-agent "Can I drive from Reykjavík to Akureyri right now?"     --conditions tests/data/faerd2017_1.snapshot.xml -v
roadroute-agent "Er fært til Ísafjarðar?" --conditions live --model claude-sonnet-5-5
```

Each run prints the answer, then the tools used, token counts (read from the API's `usage` field, not estimated) and an estimated cost.

| Choice | Why |
|---|---|
| Default model `claude-haiku-5-5`, effort `low` | Cheap while developing (a run is a fraction of a cent). Run the evals on Sonnet/Opus to compare quality against cost. |
| System prompt is a constant; the current time goes in the first user message | Keeps the prompt byte-identical across runs so it can be served from the prompt cache. |
| Automatic prompt caching (`cache_control`) | Each call in the loop resends the whole conversation; caching makes the repeated prefix cheaper. |
| Refusal fallback (`fallbacks: "default"`, beta) on Sonnet 5.5 / Opus 5.5 | If a safety classifier declines a request, the API re-runs it on a fallback model instead of the run ending. Not available on Haiku. `models_used` in the run record shows which model answered. |
| Tool results trusted as data only | The prompt says so, and an eval scenario puts an injected instruction in the feed to check it. |

### Why a separate package in the same repo

- `roadroute` stays a plain library with no LLM dependency. It can be tested, reused, or wrapped in an MCP server without pulling in the Anthropic SDK (`pip install roadroute` vs `pip install "roadroute[agent]"`).
- The dependency points one way: the agent imports the tools; the tools know nothing about the agent.
- One repo, because the two change together (a tool output change usually needs a prompt change) and there is only one consumer. Splitting into two repos would add versioning work with no benefit yet.

## Evals (`evals/`)

Unit tests check the loop's mechanics against a scripted fake model (`tests/test_agent_loop.py`, free, run with `pytest`). Evals check whether the **real model** behaves well, cost money, and are run on purpose:

```bash
python evals/run_evals.py --dry-run                       # list scenarios, no API calls
python evals/run_evals.py                                 # Haiku, 1 trial each
python evals/run_evals.py --model claude-sonnet-5-5 --trials 3
python evals/run_evals.py --only safety                   # by scenario id or tag
```

Every scenario uses the committed conditions snapshot and a fixed clock, so tool results are identical on every run and only the model varies. Closures and a prompt-injection are created by patching the snapshot (`evals/scenarios.py`).

Checks are deterministic code (`evals/graders.py`), with their own tests (`tests/test_eval_graders.py`):

| Check | Fails when |
|---|---|
| `finished` | the run hit the iteration cap, `max_tokens` or a refusal |
| `route_tool_succeeded` | the model answered without a successful route lookup |
| `states_data_time` | none of the `data_as_of` clock times appear in the answer |
| `km_figures_grounded` | the answer gives a distance that is not in any tool result (±1.5 km) |
| `official_source` | no pointer to umferdin.is / 1777 |
| scenario-specific | e.g. a closed pass isn't named, an injected link is repeated, an Icelandic question gets an English answer |

Results are saved to `evals/results/` with full transcripts, token counts and cost, so prompt or model changes can be compared run against run.

### Results so far (Haiku 5.5, effort `low`)

| Run | Passed | Cost | What changed |
|---|---|---|---|
| 1 | 6/9 (first reported as 8/9; re-graded) | $0.005 | Baseline. Three answers said "13:11 UTC (14:11 local time)", which is wrong: Iceland is UTC+0. The injection run was wrongly failed: the model quoted the injected text in order to warn about it. |
| 2 | 27/27 (3 trials each) | $0.009 | Tool output now says UTC is Iceland local time (a note next to the data, not only in the system prompt). Added the `no_shifted_local_time` check; the injection check now asks whether the answer still reports the route correctly instead of searching for the phrase. |

About 4,200 input and 470 output tokens per run, roughly $0.0003-0.0006 on Haiku. Prompt caching serves the system prompt and tool definitions (~1,500 tokens) from cache.

### Haiku 5.5 vs Sonnet 5.5 (effort `low`, 9 scenarios x 3 trials)

| | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Runs passed | 27/27 | 27/27 |
| Cost per run | $0.00034 | $0.0066 (~19x) |
| Median latency | 3.6 s | 4.7 s |
| Median answer length | 85 words | 112 words |
| Output tokens per run (median) | 443 | 366 |

Neither model failed a check, so **the eval set no longer separates them**: it is saturated. Reading the answers shows differences the checks don't measure:
- Sonnet gives the range of segment registration times as well as the feed time, says it is not an official source, and points to vedur.is for wind on exposed stretches.
- For Tórshavn, Sonnet called the tool and passed on its suggestion that the user might mean Þórshöfn (a town in NE Iceland). Haiku answered from general knowledge without calling the tool. Both answers are correct.
- Both models use outside knowledge in places (the Smyril Line ferry, "Hafnarfjall is often windy"). All place names in the route descriptions were checked and do come from the tool data.

For what the checks measure, Haiku is enough at 1/19 of the cost. To justify a bigger model, the evals need harder cases that Haiku actually fails.

The cost of trial 1 is about 2.5x that of trials 2-3: trial 1 writes each scenario's prefix to the prompt cache and later trials read it. Real traffic doesn't repeat questions, so trial 1 is closer to production cost.

### Harder scenarios (7 more, tag `hard`)

Added after both models passed everything: several cautions on one route (camper van, crosswinds, a tunnel height limit, ice), no passable route at all, a lorry over the 5-ton axle limit, a false premise ("Öxnadalsheiði is closed, right?" when it's open), a two-leg trip, Thorshofn (Þórshöfn in Iceland, not Tórshavn), and Hveravellir (not in the gazetteer; the tool suggests the wrong places).

| | Haiku 5.5 | Sonnet 5.5 |
|---|---|---|
| Runs passed (3 trials) | 21/21 (first reported 20/21; re-graded) | 21/21 |
| Cost for the 21 runs | $0.010 | $0.157 |

Haiku's one failure was the grader again: "No, not by any route right now" didn't match the pattern for "no route". **The harder scenarios still don't separate the models.** Most of the hard work happens in code (routing, closures, timestamps), so what's left for the model is reading a compact result and explaining it, which Haiku does well.

The differences are in judgment on cases the tool doesn't cover, and the checks don't measure them:
- **Hveravellir.** Haiku called the tool with coordinates it remembered (`64.8667,-19.5667`, which are about right), found that Kjalvegur is impassable and answered from that data. Sonnet said the place isn't in the data and added, from general knowledge, that the Kjölur road is normally closed in winter. Haiku's answer is more useful, but it depends on coordinates the model recalled rather than looked up.
- **The lorry.** Both flagged the 5-ton axle limit on Svínvetningabraut. Haiku said it couldn't confirm an alternative from the data. Sonnet suggested staying on Route 1 between Blönduós and Varmahlíð. That's true on the ground but not in the data: our geometry has no Ring Road segment there (see the table at the top of this README), so the suggestion can't be checked with the tool.

Tool gaps the hard scenarios exposed:
- **No weight-aware routing.** The tool can report a weight limit but can't route around it. A `max_axle_tonnes` input would let the model answer the lorry question from data.
- **Silent detours around unknown roads.** While building `several_cautions` I set Holtavörðuheiði to `OTHEKKT` (unknown). The router avoided it, which is right, but the result gave no reason for the 413 km route instead of 380 km. `shorter_route_blocked` only reports closed segments, not penalised ones.

What the evals don't cover yet:
- `typo_recovery` doesn't exercise recovery: the model fixes "akureyrii" itself before calling the tool. A misspelling that only the gazetteer's suggestion can resolve is needed.
- Answers are checked by patterns, not judged as a whole. An answer can pass every check and still be misleading, or add claims that aren't in the data. An LLM judge scoring against a rubric would catch more.
- Judgment on cases outside the data (recalled coordinates, general knowledge about roads). Grading that needs an LLM judge with a rubric; pattern checks can't see it.

## Tests

```bash
pytest                                              # 135 unit tests (synthetic network, fake model), ~4 s
ROADROUTE_GEOMETRY_XML=path/to/faerdferlar2017_1.xml pytest   # + 8 integration tests on the real data
```

Unit tests need no network or data files. Integration tests use the committed conditions snapshot (2026-11-06) so results are stable.

## What the data taught us (and what the code does about it)

| Finding | Handling |
|---|---|
| The geometry export stores each stretch of road **~4 times at different levels of detail** (same endpoints, 4 / 13 / 46 / 125 vertices). Raw total ≈ 39,800 km; collapsed ≈ 10,200 km. | `collapse_duplicate_lines` keeps the most detailed copy. Without it Dijkstra picks the coarsest copy and under-reports distances. Lines that share endpoints but differ in length by >15% are both kept. |
| Pieces of different segments meet metres apart, not at shared vertices. | Endpoints snap to any vertex within 10 m. Swept on the real data: 3 m leaves 15 segments cut off; 10 m connects the worst; 25 m creates 2,600 joins that risk linking roads that are merely near each other. |
| 8 segments stay disconnected even at 100 m (e.g. Oddsskarðsvegur, Helgustaðavegur). | Gaps in Vegagerðin's own data. `roadroute build` lists them; routes to/through them are not possible. |
| The Ring Road (road no. 1) has a ~24 km gap between Blönduós and Vatnsskarð, bridged by Svínvetningabraut (731). | Nothing to fix; routes there pass through it. One of those segments carries a 5-ton axle limit, reported as a `restriction`. |
| Shortest-path alone sends Reykjavík → Egilsstaðir across the Sprengisandur highland tracks (533 km), which are impassable. | Costs come from live conditions: impassable/closed segments are forbidden, unknown or out-of-service ones are heavily penalised. Result reports `shorter_route_blocked`. |
| A town-centre start point can snap onto an out-of-service municipal street. | Start/end points snap to the nearest *usable* road when one is near. |
| `AstandVidbotaruppl` is a secondary **status code**, not free text; one segment carries `ALLUR_AKSTUR_BANN` (all driving banned). | Treated as blocking. |

## Cost model (`costs.py`)

Blocked for a standard car: `OFAERT_*`, `LOKAD`, `ALLUR_AKSTUR_BANN`, `FAERT_FJALLABILUM` (allowed with `vehicle="4x4"`). Penalised: `OTHEKKT` ×3, `EKKI_I_THJONUSTU` ×8, unrecognised status codes ×2 (and flagged `unknown` in the output, so a new code is visible rather than silently trusted), roads not ploughed ×2, side roads (road no. ≥ 100) ×1.1.

**These multipliers are heuristics I chose, not Vegagerðin policy.** They are tuned only by the tests above; adjust them in `CostModel` if the routes look wrong to you.

## Tool output

`get_route_conditions` returns: `total_km`, `issues` (full detail only for segments that aren't plainly passable or have a restriction), `segments` as `[name, km, status]` rows in driving order, `data_as_of`, `shorter_route_blocked` when closures forced a detour, and `notes`. Errors are JSON `{"error": ...}` results so an agent can recover (e.g. retry with a suggested place name). A Reykjavík → Akureyri result is ~2,100 characters.

`registered_at` timestamps say when a condition was last *registered*. Vegagerðin updates a condition when it changes, so an old timestamp means "unchanged since", not necessarily "stale" — the tool reports the timestamps and doesn't judge.

## Known limitations

- **Current conditions only.** No forecast. Pair with a weather tool for "tomorrow".
- **The live feed fetch is only tested against a mock server.** The URL in `conditions.py` comes from Vegagerðin's 2019 documentation and matches the downloaded snapshot, but no automated test calls it live.
- **Distances run slightly short** of familiar driving figures (Reykjavík → Akureyri comes out at ~380 km; I remember ~390 km but did not verify against a map). I haven't pinned down why.
- **Gazetteer coordinates are approximate** (~60 towns, hand-entered). The integration test checks each lands within 3 km of a mapped road, not that it sits on the right street.
- **Not turn-by-turn navigation**, no travel times, no ferries, no one-way or turn restrictions.
- **The tool description and system prompt have not been evaluated with a real model yet.** The eval harness is ready (`evals/`); run it to get a baseline.

## Data licence

Road data: **Vegagerðin**, datasets `faerd2017_1` (road conditions; downloaded 2026-11-06) and `faerdferlar2017_1` (segment geometry; downloaded November 2026), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `data/network.json.gz` is derived from the geometry, and the eval scenarios change the conditions data to create fictional test cases. [DATA_LICENSE.md](DATA_LICENSE.md) lists every change. This project is not affiliated with or endorsed by Vegagerðin.

The raw geometry export isn't in the repo (12.9 MB). To rebuild the network or run the integration tests, download layer `gis:faerdferlar2017_1` from Vegagerðin's GeoServer at `http://gagnaveita.vegagerdin.is/geoserver` (WFS).

## Layout

```
src/roadroute/
  geometry.py    parse the WFS export; collapse level-of-detail duplicates
  conditions.py  parse/fetch the live feed; TTL cache with last-good fallback
  network.py     graph build, snapping, components, save/load, nearest-node
  costs.py       status → cost multiplier (standard / 4x4 profiles)
  router.py      Dijkstra with per-segment multipliers
  gazetteer.py   place names → coordinates (accent-insensitive, typo suggestions)
  service.py     RoutePlanner: the compact result
  tools.py       tool schemas + Toolbox.call() that never raises
  cli.py         build / route / places
src/roadroute_agent/
  loop.py        the hand-written tool-use loop
  prompts.py     system prompt (constant, cacheable) + per-run time preamble
  config.py      model, effort, iteration cap, fallbacks
  usage.py       token accounting and cost estimate
  cli.py         roadroute-agent "question"
evals/
  scenarios.py   questions, snapshot patches, checks per scenario
  graders.py     deterministic checks
  run_evals.py   run against the real model, print table, save results
```
