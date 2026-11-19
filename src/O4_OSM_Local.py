################################################################################
# Offline replacement for the Overpass API, reading a local .osm.pbf file
# (e.g. a regional extract from https://download.geofabrik.de).
#
# It answers the small subset of Overpass QL used by Ortho4XP, i.e. unions of
#   node|way|rel ["key"] or ["key"="value"] (several filters are and-ed)
# restricted to a bbox, followed by (._;>>;);out meta;  and returns an XML
# answer in the same layout as Overpass, as expected by
# O4_OSM_Utils.OSM_layer.update_dicosm.
#
# The source is either a single .osm.pbf file, or a directory of them, in which
# case all files whose header bounding box intersects the tile are used and
# merged (a tile often spans several Geofabrik regions).
# For each tile, the objects of the (possibly large) source files which touch
# the tile are first extracted once into a small .osm.pbf in the tmp directory,
# all queries for that tile are then answered from that extract.
#
# Differences with Overpass: a way is selected if one of its nodes lies in the
# bbox (Overpass also selects ways whose segments merely cross it), and nested
# relations are not completed (Ortho4XP ignores relation members anyway).
#
# The node scan runs at roughly 0.4 million nodes per second, so prefer the
# smallest regional extracts covering the tile over whole-country files.
#
# Requires pyosmium (pip install osmium).
################################################################################
import os
import re
import glob
import hashlib
import time
from xml.sax.saxutils import escape
import O4_UI_Utils as UI
import O4_File_Names as FNAMES

try:
    import osmium
    has_osmium = True
except ImportError:
    has_osmium = False

_filter_re = re.compile(r'\["([^"]+)"(?:="([^"]*)")?\]')
_types = {"node": "n", "way": "w", "rel": "r"}
_bbox_hits_cache = {}  # extract file name -> IdTracker of in-bbox objects


################################################################################
def get_osm_data(query, bbox, source):
    """Same contract as O4_OSM_Utils.get_overpass_data: returns the XML answer
    as bytes, or 0 on failure. bbox is (south, west, north, east)."""
    if not has_osmium:
        UI.lvprint(
            0,
            "ERROR: osm_pbf_source is set but pyosmium is not installed",
            "(pip install osmium).",
        )
        return 0
    try:
        conditions = _parse_query(query)
    except ValueError as e:
        UI.lvprint(0, "ERROR: local OSM source cannot answer query:", e)
        return 0
    pbf_files = _find_pbfs(source, bbox)
    if not pbf_files:
        return 0
    try:
        extract = _tile_extract(pbf_files, bbox)
        if UI.red_flag:
            return 0
        return _answer(extract, bbox, conditions)
    except Exception as e:
        UI.lvprint(0, "ERROR: could not read OSM data from", source, ":", e)
        return 0


################################################################################
def _parse_query(query):
    # -> list of (osm_type, [(key, value or None), ...])
    # value is None for ["key"] (key present, any value)
    conditions = []
    for q in [query] if isinstance(query, str) else query:
        (osm_type, _, filters) = q.strip().partition("[")
        filters = "[" + filters
        matches = list(_filter_re.finditer(filters))
        if (
            osm_type not in _types
            or not matches
            or "".join(m.group(0) for m in matches) != filters
        ):
            raise ValueError(q)
        conditions.append(
            (_types[osm_type], [(m.group(1), m.group(2)) for m in matches])
        )
    return conditions


################################################################################
def _header_box(pbf_file):
    reader = osmium.io.Reader(pbf_file, osmium.osm.osm_entity_bits.NOTHING)
    try:
        box = reader.header().box()
    finally:
        reader.close()
    if not box.valid():
        return None
    return (
        box.bottom_left.lat,
        box.bottom_left.lon,
        box.top_right.lat,
        box.top_right.lon,
    )


def _find_pbfs(source, bbox):
    if os.path.isfile(source):
        return [source]
    if not os.path.isdir(source):
        UI.lvprint(0, "ERROR: osm_pbf_source", source, "does not exist.")
        return []
    (s, w, n, e) = bbox
    pbf_files = []
    for f in sorted(glob.glob(os.path.join(source, "*.osm.pbf"))):
        box = _header_box(f)
        # files without a header bbox are always used
        if not box or (
            box[0] < n and box[2] > s and box[1] < e and box[3] > w
        ):
            pbf_files.append(f)
    if not pbf_files:
        UI.lvprint(
            0,
            "ERROR: no .osm.pbf file in",
            source,
            "intersects the tile",
            FNAMES.short_latlon(s, w),
            "(based on the bounding box in the file header).",
        )
    return pbf_files


################################################################################
def _bbox_hits(osm_file, bbox):
    # Objects which Overpass would select for bbox: nodes inside it, ways with
    # a node inside it, relations with a member among those.
    # Node pass first, it dominates the run time on large files: compare
    # integer coordinates (1e-7 deg), an invalid location is out of any bbox.
    (s, w, n, e) = (round(x * 10**7) for x in bbox)
    hits = osmium.IdTracker()
    add_node = hits.add_node
    for i, o in enumerate(osmium.FileProcessor(osm_file, osmium.osm.NODE)):
        loc = o.location
        if s <= loc.y <= n and w <= loc.x <= e:
            add_node(o.id)
        if not i & 0xFFFFF and UI.red_flag:
            return hits
    # Ways come before relations in sorted files, so one pass is enough.
    for o in osmium.FileProcessor(
        osm_file, osmium.osm.WAY | osmium.osm.RELATION
    ):
        if hits.contains_any_references(o):
            if o.is_way():
                hits.add_way(o.id)
            else:
                hits.add_relation(o.id)
    return hits


class _MergeWriter:
    def __init__(self, writer):
        self.node = self.way = self.relation = writer.add


def _tile_extract(pbf_files, bbox):
    # The extract name depends on the source files (and their versions), so
    # that updating or adding a source file triggers a new extraction.
    signature = hashlib.sha1(
        repr(
            [
                (os.path.abspath(f), os.path.getsize(f), os.path.getmtime(f))
                for f in pbf_files
            ]
            + [bbox]
        ).encode("utf-8")
    ).hexdigest()[:12]
    extract = os.path.join(
        FNAMES.Tmp_dir,
        "OSM_" + FNAMES.short_latlon(bbox[0], bbox[1]) + "_"
        + signature + ".osm.pbf",
    )
    if os.path.isfile(extract):
        return extract
    parts = []
    for i, pbf_file in enumerate(pbf_files):
        UI.vprint(
            1,
            "    * Extracting tile data from",
            os.path.basename(pbf_file),
            "({:.0f} MB, done once per tile)...".format(
                os.path.getsize(pbf_file) / 2**20
            ),
        )
        timer = time.time()
        hits = _bbox_hits(pbf_file, bbox)
        if UI.red_flag:
            return extract
        hits.complete_backward_references(pbf_file, relation_depth=1)
        part = extract + "." + str(i) + ".part.osm.pbf"
        with osmium.SimpleWriter(part, overwrite=True) as writer:
            for o in osmium.FileProcessor(pbf_file).with_filter(
                hits.id_filter()
            ):
                writer.add(o)
        parts.append(part)
        UI.vprint(1, "      done in {:.0f} sec.".format(time.time() - timer))
    if len(parts) == 1:
        os.replace(parts[0], extract)
    else:
        # objects present in several source files are written only once
        merged = extract + ".merged.part.osm.pbf"
        reader = osmium.MergeInputReader()
        for part in parts:
            reader.add_file(part)
        with osmium.SimpleWriter(merged, overwrite=True) as writer:
            reader.apply(_MergeWriter(writer), simplify=True)
        os.replace(merged, extract)
        for part in parts:
            os.remove(part)
    _bbox_hits_cache.pop(extract, None)
    return extract


################################################################################
def _matches(o, filters):
    for (k, v) in filters:
        if k not in o.tags:
            return False
        if v is not None and o.tags[k] != v:
            return False
    return True


def _answer(extract, bbox, conditions):
    if extract not in _bbox_hits_cache:
        _bbox_hits_cache[extract] = _bbox_hits(extract, bbox)
    hits = _bbox_hits_cache[extract]
    keys = {k for (_, filters) in conditions for (k, _) in filters}
    selection = osmium.IdTracker()
    add = {
        "n": selection.add_node,
        "w": selection.add_way,
        "r": selection.add_relation,
    }
    for o in (
        osmium.FileProcessor(extract)
        .with_filter(osmium.filter.KeyFilter(*keys))
        .with_filter(hits.id_filter())
    ):
        osm_type = o.type_str()
        for (t, filters) in conditions:
            if t == osm_type and _matches(o, filters):
                add[t](o.id)
                break
    # equivalent of (._;>>;) : all members and nodes of the selection
    selection.complete_backward_references(extract, relation_depth=1)
    return _to_xml(extract, selection)


################################################################################
def _attr(value):
    return escape(value, {'"': "&quot;"}).replace("\n", "&#10;")


def _tags_xml(o, out):
    for t in o.tags:
        out.append(
            '    <tag k="' + _attr(t.k) + '" v="' + _attr(t.v) + '"/>\n'
        )


def _to_xml(extract, selection):
    # Same line layout as an Overpass answer, which is what the line based
    # parser in O4_OSM_Utils expects.
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<osm version="0.6" generator="Ortho4XP local pbf">\n',
    ]
    written_nodes = set()
    written_ways = set()
    for o in osmium.FileProcessor(extract).with_filter(selection.id_filter()):
        if o.is_node():
            loc = o.location
            if not loc.valid():
                continue
            head = (
                '  <node id="' + str(o.id) + '" lat="'
                + "{:.7f}".format(loc.lat) + '" lon="'
                + "{:.7f}".format(loc.lon) + '"'
            )
            if len(o.tags):
                out.append(head + ">\n")
                _tags_xml(o, out)
                out.append("  </node>\n")
            else:
                out.append(head + "/>\n")
            written_nodes.add(o.id)
        elif o.is_way():
            # drop references to nodes missing from the source extract, the
            # parser would choke on them
            refs = [r.ref for r in o.nodes if r.ref in written_nodes]
            if len(refs) < 2:
                continue
            out.append('  <way id="' + str(o.id) + '">\n')
            for ref in refs:
                out.append('    <nd ref="' + str(ref) + '"/>\n')
            _tags_xml(o, out)
            out.append("  </way>\n")
            written_ways.add(o.id)
        elif o.is_relation():
            out.append('  <relation id="' + str(o.id) + '">\n')
            for m in o.members:
                if m.type == "w" and m.ref not in written_ways:
                    continue
                if m.type == "n" and m.ref not in written_nodes:
                    continue
                mtype = {"n": "node", "w": "way", "r": "relation"}[m.type]
                out.append(
                    '    <member type="' + mtype + '" ref="' + str(m.ref)
                    + '" role="' + _attr(m.role) + '"/>\n'
                )
            _tags_xml(o, out)
            out.append("  </relation>\n")
    out.append("</osm>\n")
    return "".join(out).encode("utf-8")
