# OSM data source fix

## Diagnosis (2026-11-19)
Probed every server in `OSM.overpass_servers` with a tiny 2 km bbox query:
- `overpass-api.de`: **406 Not Acceptable** for the default `python-requests` User-Agent. With a real UA it answers, but is currently at capacity (502).
- `api.openstreetmap.fr/oapi`: connection refused (dead). Its successor `overpass.openstreetmap.fr` returns 403 "only available to white-listed usages".
- `overpass.osm.rambler.ru`: connection refused (dead).
- `overpass.kumi.systems`: read timeout, even after 100 s.
- Not in the list: `overpass.private.coffee` returns 429 "include a meaningful User-Agent" (timeout with one); `maps.mail.ru` returns 504.

=> Two root causes: (1) Ortho4XP sends no User-Agent, which is now rejected; (2) public Overpass capacity is unreliable.

## Plan
### Part A: fix Overpass client (small)
- [x] Send a descriptive `User-Agent` header on every Overpass request
- [x] Use POST (`data=` form field) instead of a long GET URL, with an `[out:xml][timeout:180]` header
- [x] Update `overpass_servers`: drop dead FR/RU, add `overpass.private.coffee`; keep DE, KU
- [x] Report the HTTP status/body snippet on failure instead of the generic "rejected"/"too busy"

### Part B: offline source from a Geofabrik `.osm.pbf` extract (new)
- [x] New config var `osm_pbf_source` (Application section; file or directory; empty = use Overpass)
- [x] New `src/O4_OSM_Local.py` (pyosmium): for a tile bbox, extract once into `tmp/` an XML file equivalent to Overpass `(._;>>;);out meta;`, then answer each `node|way|rel[...]` query from it
- [x] Output in the exact line layout `OSM_layer.update_dicosm` parses (nodes → ways → relations, complete ways/relations)
- [x] Hook into `get_overpass_data` so the cache files in `OSM_data/` keep working unchanged
- [x] Add `osmium` to `requirements.txt` as an optional dependency

### Verification
- [x] Part A: rerun the probe (Overpass verified with the comparison queries; a full Step 1 through Overpass was not run)
- [x] Part B: build Step 1 (`VMAP.build_poly_file`) for a test tile from a small Geofabrik extract; compare object counts with Overpass output for the same query where possible

## Review (2026-11-19)
- Part A: with the User-Agent fix, overpass-api.de answered all comparison queries (40-95 s each, server is loaded). Without it: 406.
- Part B, verified against Overpass on bbox (47.12, 9.52, 47.18, 9.56) inside the Liechtenstein extract: identical node/way/relation IDs for `way[highway=primary]`, `way[natural=water]`, `rel[landuse=forest]` (multipolygons incl. members), aeroway union, `way[building=yes]`.
  Known differences: relations whose members extend outside the source extract (e.g. national boundaries) are incomplete; nested sub-relations are not returned (Ortho4XP ignores them); ways crossing the bbox with no node inside are not selected.
- Directory mode: two overlapping copies of the same extract merge to byte-identical output as the single file.
- Full Step 1 (`VMAP.build_poly_file`) for tile +47+009 from the local source: success, normal `OSM_data` cache files written.
- Config window builds with the new field; `apply_changes` sets `OSM.osm_pbf_source`.
- Performance: node scan ~0.4 M nodes/s (pure Python per node, pyosmium has no C++ bbox filter) -> prefer small regional extracts. Once per tile.
- Bug found and fixed during testing: `complete_backward_references(relation_depth=0)` does not add relation members; needs `relation_depth=1`.
