#!/usr/bin/env python
"""
Deterministic Minecraft asset-spec -> .bbmodel generator.

Pipeline (NONE of this goes through an LLM):
    asset spec JSON  ->  validate_schema
                     ->  validate_geometry (archetype proportion anchors)
                     ->  build .bbmodel (grid-snapped cuboids, hierarchy, UVs)
                     ->  generate texture atlas (Pillow, deterministic)
                     ->  validate_bbmodel (faces, UV bounds, ASCII, detail binding)
                     ->  write .bbmodel (+ optional atlas PNG)

Usage:
    python generate_bbmodel.py spec.json -o out.bbmodel [--png out.png] [--strict]

Exit codes: 0 ok, 2 validation error.
Only hard dependency for geometry/validation: stdlib.
Texture rendering additionally needs Pillow; without it, --no-texture still works.
"""
import argparse
import base64
import io
import json
import math
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS = os.path.join(os.path.dirname(HERE), "presets", "archetypes.json")
GRID = 0.5  # snap resolution in px; kills float noise / "auto-reconstructed mesh" look

ASCII_RE = re.compile(r"^[\x00-\x7F]*$")
FACE_DIRS = ("north", "south", "east", "west", "up", "down")


class SpecError(Exception):
    def __init__(self, errors):
        self.errors = errors if isinstance(errors, list) else [errors]
        super().__init__("\n".join(str(e) for e in self.errors))


def _snap(v, grid=GRID):
    return round(round(v / grid) * grid, 4)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Layer 1: schema validation (lightweight, no jsonschema dependency)
# ---------------------------------------------------------------------------
def validate_schema(spec):
    errs = []

    def req(obj, key, where):
        if key not in obj:
            errs.append(f"[schema] missing '{key}' in {where}")
            return False
        return True

    if not isinstance(spec, dict):
        raise SpecError("[schema] top-level spec must be an object")

    req(spec, "meta", "spec")
    req(spec, "target", "spec")
    req(spec, "atlas", "spec")
    req(spec, "parts", "spec")

    meta = spec.get("meta", {})
    if isinstance(meta, dict):
        name = meta.get("name", "")
        if not name:
            errs.append("[schema] meta.name is required")
        elif not ASCII_RE.match(str(name)):
            errs.append(f"[schema] meta.name must be ASCII, got {name!r}")

    target = spec.get("target")
    if target not in ("block", "entity", "item"):
        errs.append(f"[schema] target must be block|entity|item, got {target!r}")

    atlas = spec.get("atlas", {})
    if isinstance(atlas, dict):
        for dim in ("width", "height"):
            v = atlas.get(dim)
            if v is None:
                errs.append(f"[schema] atlas.{dim} required (int or 'auto')")
            elif v != "auto" and not (isinstance(v, int) and v >= 1):
                errs.append(f"[schema] atlas.{dim} must be positive int or 'auto', got {v!r}")

    parts = spec.get("parts", [])
    if not isinstance(parts, list) or not parts:
        errs.append("[schema] parts must be a non-empty array")
        raise SpecError(errs)

    ids = set()
    for i, p in enumerate(parts):
        where = f"parts[{i}]"
        pid = p.get("id")
        if not pid:
            errs.append(f"[schema] {where} missing id")
        else:
            if not ASCII_RE.match(str(pid)):
                errs.append(f"[schema] {where} id must be ASCII: {pid!r}")
            if pid in ids:
                errs.append(f"[schema] duplicate part id {pid!r}")
            ids.add(pid)
        for key in ("size", "pos"):
            v = p.get(key)
            if not (isinstance(v, list) and len(v) == 3 and all(isinstance(n, (int, float)) for n in v)):
                errs.append(f"[schema] {where}.{key} must be 3 numbers, got {v!r}")
        if isinstance(p.get("size"), list) and any((n or 0) < 0 for n in p.get("size", [])):
            errs.append(f"[schema] {where}.size has negative dimension")

    # parent references must resolve
    for i, p in enumerate(parts):
        par = p.get("parent")
        if par not in (None, "") and par not in ids:
            errs.append(f"[schema] parts[{i}] parent {par!r} not found")

    # face_details references
    for i, d in enumerate(spec.get("face_details", []) or []):
        where = f"face_details[{i}]"
        if "id" not in d:
            errs.append(f"[schema] {where} missing id")
        if d.get("part") not in ids:
            errs.append(f"[schema] {where} part {d.get('part')!r} not found")
        kind = d.get("kind")
        if kind not in ("texture", "geometry"):
            errs.append(f"[schema] {where} kind must be texture|geometry, got {kind!r}")
        if kind == "texture":
            if d.get("face") not in FACE_DIRS:
                errs.append(f"[schema] {where} texture detail needs a valid face")
            uv = d.get("uv")
            if not (isinstance(uv, list) and len(uv) == 4):
                errs.append(f"[schema] {where} texture detail needs uv [x,y,w,h]")

    if errs:
        raise SpecError(errs)
    return True


# ---------------------------------------------------------------------------
# Layer 2: geometry sanity via archetype proportion anchors
# ---------------------------------------------------------------------------
class _Dim:
    """Wraps a part's size so rules can read .w/.h/.d."""
    def __init__(self, size):
        self.w, self.h, self.d = (size + [0, 0, 0])[:3]


def _load_presets():
    try:
        return load_json(PRESETS)
    except Exception:
        return {}


def _bbox(parts):
    xs = []
    ys = []
    zs = []
    for p in parts:
        px, py, pz = p["pos"]
        sw, sh, sd = p["size"]
        xs += [px, px + sw]
        ys += [py, py + sh]
        zs += [pz, pz + sd]
    return _Dim([max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)])


def _resolve_roles(arch, parts_by_id):
    """Map role name -> representative _Dim (first matching part)."""
    env = {}
    roles = arch.get("_roles", {})
    for role, candidates in roles.items():
        for cand in candidates:
            if cand in parts_by_id:
                env[role] = _Dim(parts_by_id[cand]["size"])
                break
    # also expose every part by its own id
    for pid, p in parts_by_id.items():
        env[pid] = _Dim(p["size"])
    return env


def validate_geometry(spec, strict=False):
    """Check proportions against archetype anchors.

    Default (advisory) mode: NEVER raises. Returns findings as guidance so the
    author can decide whether an unusual proportion is a mistake or intentional.
    strict mode: severity:error rules become hard failures (SpecError).
    Returns list of (severity, msg) where severity in {advice, warn, error}.
    """
    findings = []
    arch_key = spec.get("archetype")
    presets = _load_presets()
    parts = spec["parts"]
    parts_by_id = {p["id"]: p for p in parts}

    # universal checks
    for p in parts:
        sw, sh, sd = p["size"]
        if sw == 0 or sh == 0 or sd == 0:
            findings.append(("warn", f"part {p['id']} has a zero dimension (flat plane); intended?"))

    # overlap/penetration sniff between sibling parts (cheap AABB)
    def aabb(p):
        x, y, z = p["pos"]
        w, h, d = p["size"]
        return (x, y, z, x + w, y + h, z + d)

    if arch_key and arch_key in presets:
        arch = presets[arch_key]
        env = _resolve_roles(arch, parts_by_id)
        env["bbox"] = _bbox(parts)
        safe = {"abs": abs, "max": max, "min": min, "math": math}
        for rule in arch.get("rules", []):
            expr = rule["expr"]
            try:
                ok = bool(eval(expr, {"__builtins__": {}}, {**safe, **env}))  # noqa: S307 - trusted preset file
            except Exception:
                # role not present in this model -> rule not applicable
                continue
            if not ok:
                findings.append((rule.get("severity", "warn"),
                                 f"{rule['id']}: {rule.get('msg', expr)}"))
    elif arch_key:
        findings.append(("warn", f"archetype {arch_key!r} not found in presets; skipping proportion checks"))

    # Proportion findings are ADVISORY by default: they guide, they do not block.
    # Unusual proportions are frequently intentional (aliens, totems, snowmen,
    # long-eared rabbits, stylized cartoon heads). The job of this skill is to make
    # ANY asset, not to enforce one house style.
    # Only --strict promotes severity:error proportion rules to hard failures, for
    # callers who want a tight gate (e.g. batch-generating conventional mobs).
    if strict:
        hard = [m for sev, m in findings if sev == "error"]
        if hard:
            raise SpecError([f"[geometry] {m}" for m in hard])
        # under strict, surface the rest as-is
        return findings
    # advisory mode: relabel so output does not cry "error" when nothing blocked
    return [("advice", m) if sev == "error" else (sev, m) for sev, m in findings]


# ---------------------------------------------------------------------------
# Atlas: flexible / auto sizing + deterministic Pillow render
# ---------------------------------------------------------------------------
def _next_pow2(n):
    p = 1
    while p < n:
        p *= 2
    return p


def resolve_atlas_size(spec):
    """Resolve atlas width/height. 'auto' => smallest pow2 covering all regions
    and per-face UVs, min 16. Non-square allowed."""
    atlas = spec["atlas"]
    aw, ah = atlas.get("width"), atlas.get("height")
    if aw != "auto" and ah != "auto":
        return int(aw), int(ah)

    max_u = 16
    max_v = 16
    for r in atlas.get("regions", []) or []:
        x, y, rw, rh = r["rect"]
        max_u = max(max_u, x + rw)
        max_v = max(max_v, y + rh)
    for p in spec["parts"]:
        # box-uv layout footprint: origin + 2*(w+d) wide, (h+d) tall
        if p.get("uv_mode", "box") == "box":
            ou, ov = p.get("uv_origin", [0, 0])
            pw, ph, pd = p["size"]
            max_u = max(max_u, ou + 2 * (pw + pd))
            max_v = max(max_v, ov + (ph + pd))
        for fd in (p.get("faces") or {}).values():
            u1, v1, u2, v2 = fd["uv"]
            max_u = max(max_u, u1, u2)
            max_v = max(max_v, v1, v2)
    # NOTE: face_details uvs are NOT used to grow the atlas. Details paint onto
    # the existing surface; a detail uv beyond the atlas is a bug to be caught
    # by validate_bbmodel, not silently absorbed.
    rw = int(aw) if aw != "auto" else _next_pow2(int(math.ceil(max_u)))
    rh = int(ah) if ah != "auto" else _next_pow2(int(math.ceil(max_v)))
    return rw, rh


def _hex(c, default=(160, 160, 160, 255)):
    if not c:
        return default
    c = c.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) == 6:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
    if len(c) == 8:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4, 6))
    return default


def render_atlas(spec, width, height):
    """Deterministic atlas. Returns PNG bytes, or None if Pillow missing."""
    try:
        from PIL import Image
    except ImportError:
        return None
    import random

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    px = img.load()

    def fill_rect(x, y, w, h, base, palette, noise):
        rng = random.Random(hash((x, y, w, h, base)) & 0xFFFFFFFF)
        pal = [_hex(c) for c in (palette or [])] or [base]
        for yy in range(int(y), int(y + h)):
            for xx in range(int(x), int(x + w)):
                if 0 <= xx < width and 0 <= yy < height:
                    col = base
                    if noise and rng.random() < noise:
                        col = pal[rng.randrange(len(pal))]
                    px[xx, yy] = col

    for r in spec["atlas"].get("regions", []) or []:
        x, y, w, h = r["rect"]
        fill_rect(x, y, w, h, _hex(r.get("fill")), r.get("palette"), r.get("noise", 0))

    # paint texture-kind face details on top
    for d in spec.get("face_details", []) or []:
        if d.get("kind") == "texture" and d.get("uv"):
            x, y, w, h = d["uv"]
            col = _hex(d.get("color"), (40, 40, 40, 255))
            for yy in range(int(y), int(y + h)):
                for xx in range(int(x), int(x + w)):
                    if 0 <= xx < width and 0 <= yy < height:
                        px[xx, yy] = col

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Builder: spec -> .bbmodel dict
# ---------------------------------------------------------------------------
def _uuid():
    return str(uuid.uuid4())


def _box_uv_faces(p, tex_index, uv_w, uv_h):
    """Standard Blockbench box-uv layout from a single origin [u,v].
    Layout per BB convention:
        right(east)  at (u,           v+d)
        front(north) at (u+d,         v+d)
        left(west)   at (u+d+w,       v+d)
        back(south)  at (u+d+w+d,     v+d)
        top(up)      at (u+d,         v)
        bottom(down) at (u+d+w,       v)
    sizes: depth d, width w, height h (in px).
    """
    u, v = p.get("uv_origin", [0, 0])
    w, h, d = p["size"]
    faces = {}

    def f(x1, y1, x2, y2):
        return {"uv": [x1, y1, x2, y2], "texture": tex_index}

    faces["east"] = f(u, v + d, u + d, v + d + h)
    faces["north"] = f(u + d, v + d, u + d + w, v + d + h)
    faces["west"] = f(u + d + w, v + d, u + d + w + d, v + d + h)
    faces["south"] = f(u + d + w + d, v + d, u + d + 2 * w + d, v + d + h)
    faces["up"] = f(u + d, v, u + d + w, v + d)
    faces["down"] = f(u + d + w, v, u + d + 2 * w, v + d)
    return faces


def _per_face_faces(p, tex_index):
    faces = {}
    for direction, fd in (p.get("faces") or {}).items():
        faces[direction] = {"uv": list(fd["uv"]), "texture": tex_index}
    return faces


def build_bbmodel(spec, atlas_w, atlas_h, texture_png=None, texture_name="texture"):
    tex_index = 0
    elements = []
    el_by_part = {}
    z_eps = 0.01

    for p in spec["parts"]:
        x, y, z = p["pos"]
        w, h, d = p["size"]
        inflate = p.get("inflate", 0)
        # grid-snap to kill float noise; tiny z-offset only when tagged
        fx, fy, fz = _snap(x), _snap(y), _snap(z)
        tx, ty, tz = _snap(x + w), _snap(y + h), _snap(z + d)
        if p.get("z_offset_tag"):
            fz -= z_eps
            tz += z_eps
        origin = p.get("pivot", p["pos"])
        rot = p.get("rot", [0, 0, 0])

        if p.get("uv_mode") == "per_face" and p.get("faces"):
            faces = _per_face_faces(p, tex_index)
        else:
            faces = _box_uv_faces(p, tex_index, atlas_w, atlas_h)

        el = {
            "name": p["id"],
            "box_uv": p.get("uv_mode", "box") == "box",
            "type": "cube",
            "uuid": _uuid(),
            "from": [fx, fy, fz],
            "to": [tx, ty, tz],
            "origin": [_snap(origin[0]), _snap(origin[1]), _snap(origin[2])],
            "rotation": list(rot),
            "inflate": inflate,
            "uv_offset": p.get("uv_origin", [0, 0]),
            "faces": faces,
        }
        elements.append(el)
        el_by_part[p["id"]] = el["uuid"]

    outliner = _build_outliner(spec["parts"], el_by_part)

    textures = []
    if texture_png is not None:
        b64 = base64.b64encode(texture_png).decode("ascii")
        textures.append({
            "name": texture_name,
            "id": "0",
            "width": atlas_w,
            "height": atlas_h,
            "uv_width": atlas_w,
            "uv_height": atlas_h,
            "source": "data:image/png;base64," + b64,
            "internal": True,
            "saved": False,
            "uuid": _uuid(),
        })

    return {
        "meta": {"format_version": "4.5", "model_format": "free", "box_uv": False},
        "name": spec["meta"]["name"],
        "resolution": {"width": atlas_w, "height": atlas_h},
        "elements": elements,
        "outliner": outliner,
        "textures": textures,
    }


def _build_outliner(parts, el_by_part):
    """Build nested outliner groups by parent hierarchy."""
    children_of = {}
    roots = []
    for p in parts:
        par = p.get("parent") or None
        children_of.setdefault(par, []).append(p["id"])
        if par is None:
            roots.append(p["id"])

    def node_for(pid):
        kids = children_of.get(pid, [])
        if not kids:
            return el_by_part[pid]  # leaf -> just the element uuid
        grp = {
            "name": pid + "_grp",
            "uuid": _uuid(),
            "export": True,
            "isOpen": True,
            "children": [el_by_part[pid]] + [node_for(k) for k in kids],
        }
        return grp

    return [node_for(r) for r in roots]


# ---------------------------------------------------------------------------
# Layer 3: bbmodel correctness validation
# ---------------------------------------------------------------------------
def validate_bbmodel(model, spec):
    errs = []
    warns = []
    n_tex = len(model["textures"])
    aw = model["resolution"]["width"]
    ah = model["resolution"]["height"]

    for el in model["elements"]:
        if not ASCII_RE.match(el["name"]):
            errs.append(f"[bbmodel] element name not ASCII: {el['name']!r}")
        if "\ufffd" in json.dumps(el, ensure_ascii=False):
            errs.append(f"[bbmodel] replacement char in element {el['name']}")
        for direction, face in el["faces"].items():
            ti = face.get("texture")
            if n_tex == 0:
                continue
            if ti is None or ti >= n_tex or ti < 0:
                errs.append(f"[bbmodel] {el['name']}.{direction} bad texture index {ti}")
            u1, v1, u2, v2 = face["uv"]
            if min(u1, u2) < -0.001 or max(u1, u2) > aw + 0.001 or \
               min(v1, v2) < -0.001 or max(v1, v2) > ah + 0.001:
                warns.append(f"[bbmodel] {el['name']}.{direction} uv out of atlas {aw}x{ah}: {face['uv']}")

    # every texture-kind face_detail must map to a real element+face,
    # and its uv must fall inside the atlas
    el_names = {el["name"] for el in model["elements"]}
    for d in spec.get("face_details", []) or []:
        if d.get("kind") == "texture":
            if d["part"] not in el_names:
                errs.append(f"[bbmodel] face_detail {d['id']} targets missing part {d['part']}")
            x, y, w, h = d["uv"]
            if x < 0 or y < 0 or x + w > aw + 0.001 or y + h > ah + 0.001:
                errs.append(f"[bbmodel] face_detail {d['id']} uv {d['uv']} outside atlas {aw}x{ah}")
        elif d.get("kind") == "geometry":
            # geometry details must actually exist as a part, not be painted
            if d["part"] not in el_names:
                warns.append(f"[bbmodel] geometry detail {d['id']} references part {d['part']} not built")

    if "\ufffd" in json.dumps(model, ensure_ascii=False):
        errs.append("[bbmodel] replacement char (?) found in model JSON")

    if errs:
        raise SpecError(errs)
    return warns


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def generate(spec, no_texture=False, strict=False):
    """Full pipeline. Returns (model_dict, atlas_png_or_None, findings)."""
    findings = []
    validate_schema(spec)
    findings += validate_geometry(spec, strict=strict)
    aw, ah = resolve_atlas_size(spec)
    png = None if no_texture else render_atlas(spec, aw, ah)
    model = build_bbmodel(spec, aw, ah, texture_png=png,
                          texture_name=spec["meta"]["name"] + "_tex")
    findings += [("warn", w) for w in validate_bbmodel(model, spec)]
    return model, png, findings


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic asset-spec -> .bbmodel generator")
    ap.add_argument("spec", help="Path to asset spec JSON")
    ap.add_argument("-o", "--out", required=True, help="Output .bbmodel path")
    ap.add_argument("--png", help="Also write the atlas PNG here for inspection")
    ap.add_argument("--no-texture", action="store_true", help="Skip texture generation")
    ap.add_argument("--strict", action="store_true",
                    help="Enforce archetype proportion rules as hard errors. "
                         "Off by default: proportions are advisory only.")
    args = ap.parse_args(argv)

    spec = load_json(args.spec)
    try:
        model, png, findings = generate(spec, no_texture=args.no_texture, strict=args.strict)
    except SpecError as e:
        print("VALIDATION FAILED:", file=sys.stderr)
        for line in e.errors:
            print("  -", line, file=sys.stderr)
        return 2

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)
    if png and args.png:
        with open(args.png, "wb") as f:
            f.write(png)

    print(f"OK  {args.out}")
    print(f"    parts={len(model['elements'])}  atlas={model['resolution']['width']}x{model['resolution']['height']}  textures={len(model['textures'])}")
    labels = {"advice": "proportion note (not blocking)", "warn": "warn", "error": "error"}
    for sev, msg in findings:
        print(f"    [{labels.get(sev, sev)}] {msg}")
    if any(sev == "advice" for sev, _ in findings):
        print("    (proportion notes are advisory. If the look is intentional, ignore them. Use --strict to enforce.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

