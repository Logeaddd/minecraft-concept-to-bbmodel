#!/usr/bin/env python
"""
Reverse a .bbmodel back into an asset spec (the intermediate format consumed by
generate_bbmodel.py).

Use this to keep iterating on a model you (or someone else) already started:
    .bbmodel  ->  bbmodel_to_spec.py  ->  asset spec  ->  edit  ->  generate_bbmodel.py

What round-trips faithfully:
    - cuboid geometry: pos (from), size (to-from), pivot (origin), rotation, inflate
    - part hierarchy / parenting (from the outliner groups)
    - UV: box-uv origin or explicit per-face UVs
    - atlas resolution

What is LOSSY / cannot be recovered (documented, not silently dropped):
    - texture PIXELS: the spec only describes atlas 'regions' + 'face_details', which
      cannot be inferred from a finished PNG. Use --dump-texture to save the embedded
      texture(s) to PNG so you do not lose them; re-attach by hand if needed.
    - face_details (eyes/labels/etc): we cannot tell which painted pixels were "details".
      Left empty; re-declare the important ones if you want them validated.
    - target / archetype: guessed (target by coordinate range) or left blank.

Usage:
    python bbmodel_to_spec.py model.bbmodel -o model.spec.json
    python bbmodel_to_spec.py model.bbmodel -o model.spec.json --dump-texture tex.png

Exit codes: 0 ok, 2 parse error.
"""
import argparse
import base64
import json
import os
import re
import sys

GRID = 0.5
FACE_DIRS = ("north", "south", "east", "west", "up", "down")


def _snap(v, grid=GRID):
    return round(round(v / grid) * grid, 4)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _ascii_id(name, used, fallback):
    """Sanitize an element name to a unique ASCII id."""
    s = re.sub(r"[^0-9A-Za-z_]+", "_", str(name or "")).strip("_")
    if not s or not re.match(r"^[A-Za-z_]", s):
        s = fallback if not s else "p_" + s
    base = s
    i = 2
    while s in used:
        s = f"{base}_{i}"
        i += 1
    used.add(s)
    return s


# ---------------------------------------------------------------------------
# Hierarchy recovery from the outliner tree.
# ---------------------------------------------------------------------------
def _index_elements(model):
    """uuid -> element dict."""
    return {el.get("uuid"): el for el in model.get("elements", []) if el.get("uuid")}


def recover_parents(model):
    """Walk the outliner and return {element_uuid: parent_element_uuid_or_None}.

    Convention used by generate_bbmodel.py: a group named '<id>_grp' holds its own
    element as the FIRST child (the 'anchor'), followed by child sub-trees. We
    generalize: within any group, the first cube child is the anchor for that group;
    every other element/group under it is parented to that anchor. Groups with no
    direct cube child fall back to the nearest enclosing anchor.
    """
    parents = {}
    elems = _index_elements(model)

    def first_cube_uuid(node):
        """Return the uuid of the first direct cube child of a group node."""
        for c in node.get("children", []):
            if isinstance(c, str) and c in elems:
                return c
        return None

    def walk(node, inherited_anchor):
        # node is either a uuid string (leaf cube) or a group dict
        if isinstance(node, str):
            if node in elems and node not in parents:
                parents[node] = inherited_anchor
            return
        if not isinstance(node, dict):
            return
        anchor = first_cube_uuid(node) or inherited_anchor
        # the anchor itself is parented to the inherited anchor
        if anchor and anchor in elems and anchor not in parents:
            parents[anchor] = inherited_anchor
        for c in node.get("children", []):
            if isinstance(c, str):
                if c == anchor:
                    continue
                if c in elems and c not in parents:
                    parents[c] = anchor
            else:
                walk(c, anchor)

    outliner = model.get("outliner", [])
    if outliner:
        for root in outliner:
            walk(root, None)
    # any element not reached by the outliner -> root
    for uuid in elems:
        parents.setdefault(uuid, None)
    return parents


# ---------------------------------------------------------------------------
# UV recovery.
# ---------------------------------------------------------------------------
def recover_uv(el, size):
    """Return (uv_mode, uv_origin_or_None, faces_or_None).

    Prefers an explicit box-uv origin when the model marks box_uv / uv_offset.
    Otherwise tries to reconstruct a box-uv origin from the face layout produced by
    generate_bbmodel.py; if the faces do not fit that layout, falls back to per_face.
    """
    w, h, d = size
    faces = el.get("faces", {}) or {}

    # explicit signals first
    if el.get("box_uv") and "uv_offset" in el:
        ou, ov = el["uv_offset"]
        return "box", [_snap(ou), _snap(ov)], None

    # try to reconstruct box-uv origin: in our layout the EAST face is at
    # (u, v+d) .. (u+d, v+d+h). So u = east.u1, v = east.v1 - d.
    east = faces.get("east", {}).get("uv")
    if east and d > 0:
        u = east[0]
        v = east[1] - d
        cand = [_snap(u), _snap(v)]
        if _box_uv_matches(faces, cand, w, h, d):
            return "box", cand, None

    # fall back to explicit per-face UVs
    pf = {}
    for direction in FACE_DIRS:
        f = faces.get(direction)
        if f and "uv" in f:
            pf[direction] = {"uv": [_snap(x) for x in f["uv"]]}
    if pf:
        return "per_face", None, pf
    return "box", [0, 0], None


def _box_uv_matches(faces, origin, w, h, d, tol=0.02):
    u, v = origin
    expect = {
        "east": [u, v + d, u + d, v + d + h],
        "north": [u + d, v + d, u + d + w, v + d + h],
        "west": [u + d + w, v + d, u + d + w + d, v + d + h],
        "south": [u + d + w + d, v + d, u + d + 2 * w + d, v + d + h],
        "up": [u + d, v, u + d + w, v + d],
        "down": [u + d + w, v, u + d + 2 * w, v + d],
    }
    for direction, exp in expect.items():
        f = faces.get(direction, {}).get("uv")
        if f is None:
            continue
        if any(abs(a - b) > tol for a, b in zip(f, exp)):
            return False
    return True


# ---------------------------------------------------------------------------
# Target heuristic + texture extraction.
# ---------------------------------------------------------------------------
def guess_target(elements):
    """Cheap guess: everything inside 0..16 on all axes -> block; very flat -> item;
    otherwise entity. Always overridable by the user afterwards."""
    if not elements:
        return "block"
    xs, ys, zs = [], [], []
    for el in elements:
        fr = el.get("from", [0, 0, 0])
        to = el.get("to", [0, 0, 0])
        xs += [fr[0], to[0]]
        ys += [fr[1], to[1]]
        zs += [fr[2], to[2]]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    d = max(zs) - min(zs)
    in_block = (min(xs) >= -0.001 and max(xs) <= 16.001 and
                min(ys) >= -0.001 and max(ys) <= 16.001 and
                min(zs) >= -0.001 and max(zs) <= 16.001)
    if d <= 4.001 and w <= 16.001 and h <= 16.001:
        return "item"
    if in_block:
        return "block"
    return "entity"


def dump_textures(model, out_png):
    """Save embedded texture(s) to PNG. Returns list of written paths."""
    written = []
    textures = model.get("textures", []) or []
    base, ext = os.path.splitext(out_png)
    ext = ext or ".png"
    for i, t in enumerate(textures):
        src = t.get("source", "")
        if not src.startswith("data:image/"):
            continue
        b64 = src.split(",", 1)[1] if "," in src else ""
        try:
            data = base64.b64decode(b64)
        except Exception:
            continue
        path = out_png if len(textures) == 1 else f"{base}_{i}{ext}"
        with open(path, "wb") as f:
            f.write(data)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Core conversion.
# ---------------------------------------------------------------------------
def bbmodel_to_spec(model):
    elements = model.get("elements", [])
    parents = recover_parents(model)

    used = set()
    uuid_to_id = {}
    for i, el in enumerate(elements):
        uuid_to_id[el.get("uuid")] = _ascii_id(el.get("name"), used, f"part_{i}")

    parts = []
    for i, el in enumerate(elements):
        fr = [_snap(x) for x in el.get("from", [0, 0, 0])]
        to = [_snap(x) for x in el.get("to", [0, 0, 0])]
        size = [_snap(to[j] - fr[j]) for j in range(3)]
        pid = uuid_to_id[el.get("uuid")]
        parent_uuid = parents.get(el.get("uuid"))
        part = {"id": pid, "size": size, "pos": fr}
        if parent_uuid:
            part["parent"] = uuid_to_id.get(parent_uuid)
        origin = el.get("origin")
        if origin and [_snap(o) for o in origin] != fr:
            part["pivot"] = [_snap(o) for o in origin]
        rot = el.get("rotation")
        if rot and any(abs(r) > 1e-6 for r in rot):
            part["rot"] = [_snap(r) for r in rot]
        if el.get("inflate"):
            part["inflate"] = el["inflate"]
        uv_mode, uv_origin, faces = recover_uv(el, size)
        if uv_mode == "per_face":
            part["uv_mode"] = "per_face"
            part["faces"] = faces
        else:
            part["uv_origin"] = uv_origin if uv_origin is not None else [0, 0]
        parts.append(part)

    res = model.get("resolution", {}) or {}
    spec = {
        "meta": {
            "name": _ascii_id(model.get("name", "model"), set(), "model"),
            "schema_version": 1,
            "description": "Reverse-engineered from a .bbmodel. Texture pixels, "
                           "face_details, and target/archetype are lossy; review them.",
        },
        "target": guess_target(elements),
        "atlas": {
            "width": int(res.get("width", 16)),
            "height": int(res.get("height", 16)),
        },
        "parts": parts,
        "face_details": [],
    }
    return spec


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reverse a .bbmodel into an editable asset spec.")
    ap.add_argument("bbmodel", help="Path to the .bbmodel file")
    ap.add_argument("-o", "--out", required=True, help="Output spec JSON path")
    ap.add_argument("--dump-texture", metavar="PNG",
                    help="Also extract embedded texture(s) to this PNG path "
                         "(texture pixels cannot be represented in the spec).")
    args = ap.parse_args(argv)

    try:
        model = load_json(args.bbmodel)
    except Exception as e:
        print(f"PARSE FAILED: {e}", file=sys.stderr)
        return 2
    if not isinstance(model, dict) or "elements" not in model:
        print("PARSE FAILED: not a .bbmodel (no 'elements')", file=sys.stderr)
        return 2

    spec = bbmodel_to_spec(model)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)

    tex_paths = []
    if args.dump_texture:
        tex_paths = dump_textures(model, args.dump_texture)

    n_parent = sum(1 for p in spec["parts"] if p.get("parent"))
    n_perface = sum(1 for p in spec["parts"] if p.get("uv_mode") == "per_face")
    print(f"OK  {args.out}")
    print(f"    parts={len(spec['parts'])}  parented={n_parent}  "
          f"per_face_uv={n_perface}  target(guessed)={spec['target']}  "
          f"atlas={spec['atlas']['width']}x{spec['atlas']['height']}")
    if tex_paths:
        print(f"    texture(s) saved: {', '.join(tex_paths)}")
    print("    LOSSY: texture pixels, face_details, and target/archetype are not "
          "fully recoverable. Review the spec before regenerating.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

