# Trail coverage

A local Python CLI that compares recorded activity GPS tracks with OpenStreetMap
trails. Estimate the percentage of a trail network you have covered for a selected
sport and date range, then explore **covered** and **uncovered** trail portions in
an interactive HTML map.

Uses GeoPandas/Shapely for analysis, SQLite for file-based storage, OSMnx for trail
downloads, and Folium for maps. No database server, hosted application, or account
sharing is required. Each person uses their own data directory and credentials.

## Strava API policy checkpoint

The connector is implemented, but **do not assume that personal use exempts it
from Strava's API restrictions**. The published
[API Policy](https://www.strava.com/legal/api_policy) sections 5.4 and 5.7 restrict
analytics and geographic-data storage; section 6.2 governs retention/caching.
Whether your intended use is permitted must be resolved with Strava or the
applicable terms **before enabling persistent activity ingestion**.

`strava sync` refuses to access the API unless you explicitly pass
`--policy-reviewed`. This flag acknowledges that you have resolved applicability;
it does not grant permission, override the terms, or establish compliance. The
tool does not pretend that expiring a cache makes otherwise restricted analytics
permitted. No live credentials are needed to develop or run the offline tests.

The API is used only for the authenticated athlete's recorded activities, not
other users, heatmaps, trail-map scraping, or saved/planned routes. The trail
denominator comes from **OpenStreetMap**, not Strava segments, which are not a
complete trail inventory.

## Install with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python
3.11 or newer, then from this repository:

```bash
uv sync --locked
uv run trail-coverage --help
```

Dependencies and development tools are declared in `pyproject.toml`; `uv.lock`
is committed for reproducible setup. Use `uv add PACKAGE` or
`uv add --dev PACKAGE` to change dependencies and commit both files. Do not
commit `.venv`.

## Private configuration and storage

Defaults follow your platform's user directories, **outside the repository**:

| Purpose | macOS default |
|---|---|
| Configuration | `~/Library/Application Support/trail-coverage/config.toml` |
| Database, tokens, reports | `~/Library/Application Support/trail-coverage/` |
| OSM/geocoding cache | `~/Library/Caches/trail-coverage/` |

On Linux these follow XDG directories; on Windows, platformdirs selects the
appropriate user AppData folders. `data status` prints the actual data path.
Use `--data-dir /private/path` **before the subcommand** to choose a separate
dataset, or set `TRAIL_COVERAGE_DATA_DIR`. With `--data-dir`, the default cache
is also inside that directory. `--config /private/config.toml` overrides the
configuration file location.

Copy `config.example.toml` to your config location and replace its placeholders,
or supply `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` in your environment
through your preferred secret manager. Environment credentials override the
configuration file. Set a meaningful `user_agent` identifying your application
and contact/project URL when using OSM services.

The database, tokens, and reports are private local files (mode 0600 on POSIX);
new application directories use mode 0700. This is **not encryption**, and
Windows ACLs and existing directory permissions remain your responsibility.
Do not put real credentials in shell history or committed files. Repository
ignore rules are a safety net, not a substitute for keeping personal data out
of the checkout. Never attach personal GPS data or reports to AI prompts.

## 1. Define an area and download trails

Supply exactly one input. Bounding boxes are `west,south,east,north` in WGS84
decimal degrees. The values below are illustrative, not personal activity data.

```bash
uv run trail-coverage area add example --bbox="-123.05,45.00,-123.00,45.05"
uv run trail-coverage area show example
uv run trail-coverage area refresh example
```

Or use an RFC 7946 GeoJSON Polygon/MultiPolygon, Feature, or FeatureCollection:

```bash
uv run trail-coverage area add park --geojson /path/to/boundary.geojson
```

Polygon holes and disconnected pieces are preserved. Files must contain only
polygonal area features. Invalid geometries are rejected, not silently repaired.
Area names are immutable; use a new name to change the boundary.

Place-name lookup is also supported:

```bash
uv run trail-coverage area add named-park \
  --place "A specific park, city, country" --allow-public-nominatim
```

**Review the [public Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/)
before opting in.** Public lookups are user-triggered, cached, serialized across
local processes, and limited to at most one request per second with an identifying
user agent. No autocomplete or bulk geocoding is implemented. Its application-wide
limits still apply across users/machines. Configure `nominatim_url` for another
compatible provider when needed. Never submit sensitive/private place queries.
Ambiguous polygon results fail with candidate names; point-only results do not
silently become bounding boxes.

Inspect the printed boundary/bounds before downloading. Areas are limited to
compact, non-polar extents (at most 6 degrees in longitude and latitude, within
80 S to 84 N); split larger areas. Downloads are cached, honor OSMnx's Overpass
rate handling, and fail visibly on empty or unsuccessful responses without
replacing a previous snapshot. Each successful refresh creates a new immutable
snapshot; analysis uses the newest one.

Default OSM `highway` types are `path`, `footway`, `bridleway`, and `track`.
Sidewalk-tagged footways and `area=yes` features are excluded. Replace the
download types by repeating `--highway`:

```bash
uv run trail-coverage area refresh example --highway path --highway track
```

## 2. Authorize and synchronize activities

After resolving the API policy checkpoint, register your own application in
[Strava API settings](https://www.strava.com/settings/api) and set its callback
domain to `127.0.0.1`. Supply its client ID and secret locally.

```bash
uv run trail-coverage strava auth
# Or explicitly include Only Me / privacy-zone data:
uv run trail-coverage strava auth --include-private
```

Authorization opens a browser and uses a state-validated loopback callback
(default port 8765, configurable with `--port`). It times out after three minutes.
The terminal also prints the authorization URL if the browser cannot open.
Tokens rotate automatically and are written atomically. No write scope is
requested. Without `--include-private`, the resulting history excludes data
the granted scope cannot access.

```bash
# Only after policy applicability has been resolved:
uv run trail-coverage strava sync --sport Hike --policy-reviewed
uv run trail-coverage data status
uv run trail-coverage data activities --sport Hike --limit 1000
```

Omit `--sport` to fetch GPS for all supported outdoor sport types. Repeat it to
select multiple types. All activity-list pages are discovered; GPS streams are
downloaded only for selected sports. Dates limit analysis, not history sync.
The connector uses full `latlng` and `time` streams, never a silently degraded
summary polyline, and refuses streams not labeled high-resolution by Strava.
Returned resolution, original/sample sizes, and fetch times are retained as
provenance (not a guarantee of perfect GPS sampling).
Manual/indoor/non-GPS activities are marked skipped with
reasons. Recording gaps over 120 seconds or non-increasing stream times split
tracks instead of inventing a connecting traversal.

Re-run the same sync command after an interruption: completed pages and track
imports are retained. Quota exhaustion stops with an explicit error rather than
busy-looping; resume after the actual header-reported quota resets. Transient
transport/502/503/504 errors get bounded retries. One CLI process at a time may
use a dataset, preventing refresh-token and sync-state races.

Incremental sync rechecks a seven-day overlap. Old edits, deletions, and changes
to GPS visibility require a full reconciliation:

```bash
uv run trail-coverage strava sync --sport Hike --reconcile --policy-reviewed
```

This refreshes existing metadata/GPS and deletes activities no longer visible
after a successful complete discovery scan. It cannot infer whether disappearance
means deletion or loss of permission. An interrupted scan requires the same
sport/reconcile options. To abandon it, or recover from an activity deleted
mid-scan, start a new discovery pass explicitly:

```bash
uv run trail-coverage strava sync --sport Hike --reconcile --restart --policy-reviewed
```

Changing authorization scope also requires `--reconcile --restart`, so newly
visible private history is not silently missed. Reconciliation refreshes GPS for
the selected sports; unchanged tracks for other sports are retained.

API quotas, access tiers, and permissions can change; consult the
[current documentation](https://developers.strava.com/docs/rate-limits/).
This CLI does not run webhooks or monitor revocations/deletions while closed.

## 3. Analyze and explore uncovered trails

```bash
uv run trail-coverage coverage analyze example --sport Hike
uv run trail-coverage coverage map example --sport MountainBikeRide \
  --start 2024-01-01 --end 2025-01-01 --tolerance 15 --name biking-2024
```

`analyze` and `map` both generate the same complete report bundle from one
analysis result. Open the printed HTML path in your browser. Solid green lines
are covered portions; dashed red lines are uncovered portions. Boundary and
coverage layers can be toggled. Trail popups show names, lengths, and OSM tags.
Original activity tracks are not embedded.

Reports default to the private data directory's `reports/` folder. Choose
`--output-dir /private/path` to export elsewhere, and `--name NAME` to preserve
multiple reports (the same name overwrites previous outputs):

| File | Contents |
|---|---|
| `NAME.html` | Interactive map and coverage summary |
| `NAME.json` | Metric, filters, snapshot, projection, sync/completeness metadata |
| `NAME.csv` | Per-trail total/covered/uncovered meters and percentage |
| `NAME-covered.geojson` | Covered trail portions in WGS84 |
| `NAME-uncovered.geojson` | Remaining trail portions for manual planning |

Default maps request basemap tiles and JavaScript/CSS assets from third-party
services. This can disclose IP address and viewed area. `--no-basemap` suppresses
tile requests, but CDN assets still require internet: **the HTML is not a
fully offline/self-contained map**. No reports or GPS tracks are uploaded.
Coverage exports can themselves reveal where you have been; keep them private.
No automatic route generation or navigation is included.

### Metric and filters

Coverage is `100 * unique covered trail length / unique eligible trail length`,
not the percentage of named trails touched. A partially traversed trail counts
only for its covered length; repeated/reversed trips add no extra credit.
Lines are clipped to the precise area, including holes. Coincident duplicate
linework is counted once within the same OSM layer/bridge/tunnel grouping;
distinct grade-separated trails remain separate. Shared duplicate lengths are
assigned to the first source ID for per-trail summaries.

Lengths and the default **15-meter tolerance** are measured in a local UTM CRS.
`--crs EPSG:CODE` permits a suitable explicit projected meter CRS; geographic
coordinates and Web Mercator are rejected. The area-extent restrictions still
apply. Flat-ended track buffers avoid extra credit beyond recording endpoints.
Thus an aligned 400-meter track on a 1,000-meter trail yields 40%, not 43%.
Boundary-adjacent tracks are retained for matching even if just outside the area.

This is a **proximity estimate**, not network map-matching. Nearby parallel
trails, bridges, intersections, GPS errors, simplified OSM lines, and long
sampling intervals can cause false positives/negatives. GPS does not establish
which elevation level you traveled. Inspect the map and compare tolerances;
do not treat percentages as exact ground truth.

Use exact Strava sport types. `Hike`, `Walk`, `Run`, and `TrailRun` use the `foot`
profile; `Ride`, `MountainBikeRide`, `GravelRide`, `EBikeRide`, and
`EMountainBikeRide` use `bicycle`. Unknown sports or mixed foot/bicycle selections
require an explicit `--profile foot` or `--profile bicycle` for the denominator.
Electric-bike-specific rules are not inferred.

Profiles honor explicit mode-specific access tags before general `access`.
`no`, `private`, `customers`, `delivery`, `agricultural`, and `forestry` exclude
a way; mode-specific `yes`, `designated`, `official`, `permissive`, or
`destination` can override general restrictions. Bicycle `dismount` and
`use_sidepath` are excluded. Unknown/absent tags are included, not assumed legally
verified; conditional/time-dependent access is not interpreted. Verify signs,
local law, and seasonal closures before planning.

Additional `--tag key=value` filters are exact, AND-combined, and apply to the
trail denominator, for example `--tag surface=dirt`. `--start` is inclusive and
`--end` exclusive; ISO dates and timestamps normalize to UTC. No activities means
0% if trails exist; no eligible trails means an undefined percentage. Incomplete
imports and skipped activities are reported, not silently presented as complete
history. Scope-limited API results can never establish complete private history.

## Inspect, disconnect, and delete

```bash
uv run trail-coverage data status
uv run trail-coverage data activities --limit 10000
uv run trail-coverage strava disconnect
# Local-only deletion without contacting Strava:
uv run trail-coverage data purge
```

Deletion requires confirmation (`--yes` for unattended use). Disconnect revokes
access first, then removes the managed database, tokens, and default reports.
If revocation fails, the command fails visibly; use `data purge` and revoke
access manually in Strava settings. Purge alone does not revoke remote access.
Both print confirmation when local deletion succeeds. OSM caches are not activity
data and are retained. Delete separately copied/custom-location exports, backups,
and shell output yourself. Unlinking files is not guaranteed secure erasure.

SQLite stores WGS84 WKB geometry, source IDs, import timestamps, bounding boxes,
sport/date indexes, and versioned schema information. No SpatiaLite extension is
needed. Use your own `--data-dir` per athlete; switching identities in an existing
dataset is rejected. There is no multi-user service or cross-user comparison.

## Development

```bash
uv sync --locked
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

Tests use only synthetic geometry, temporary SQLite files, and mocked Strava/
geocoding responses, including an offline CLI-to-report workflow. They do not
require a Strava account. Geospatial libraries without typing metadata have
targeted missing-stub overrides; application code remains type-checked.

Modules separate configuration, persistence, OSM acquisition, Strava acquisition,
geometry, coverage, report generation, and CLI wiring. GPX/FIT/TCX import,
network map-matching, and automated itinerary optimization are not included.

## Data attribution and support

Trail data: [© OpenStreetMap contributors](https://www.openstreetmap.org/copyright),
available under ODbL. Preserve attribution and review ODbL obligations before
redistributing OSM-derived data. Activity data: Strava; third-party/device
attribution requirements (including Garmin where applicable) must also be
respected. No affiliation or endorsement is implied. Use the repository's
[issue tracker](https://github.com/draskell/strava_complete/issues) for software
support, **without uploading personal activity data or credentials**.
