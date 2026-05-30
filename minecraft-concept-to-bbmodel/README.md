# Minecraft Concept To BBModel

Turn a text description or reference image into an editable Blockbench `.bbmodel` by
authoring a **structured asset spec** and running a **deterministic generator**.

This is deliberately *not* a one-shot "AI mesh generator" and *not* an image-to-model
trick. A language model cannot reliably estimate cuboid coordinates, UVs, and pivots, so
it doesn't. It fills a structured spec; a Python script does the exact parts.

## Why this design

Hand-writing `.bbmodel` JSON with an LLM, or reading geometry straight off a picture,
reliably produces:

- broken proportions (ears too tall, body shaped like a column),
- float noise that looks like an auto-reconstructed mesh,
- Z-fighting from stacked coplanar faces,
- texture detail painted into the atlas but never bound to the correct cuboid face.

Splitting the work removes all four at the source:

```text
input (text / image)
  -> [AI]     author asset spec JSON
  -> [script] validate schema      (structure, ids, ASCII, references)
  -> [script] validate geometry    (archetype proportion anchors)
  -> [script] build .bbmodel       (grid-snapped cuboids, hierarchy, UVs)
  -> [script] render atlas         (Pillow, deterministic, flexible size)
  -> [script] validate bbmodel     (faces, UV bounds, detail binding, encoding)
  -> .bbmodel -> open in Blockbench for polish
```

## Layout

```text
minecraft-concept-to-bbmodel/
|-- SKILL.md                       compact agent instructions
|-- README.md                      this file
|-- schema/
|   `-- asset_spec.schema.json     the intermediate spec contract
|-- presets/
|   `-- archetypes.json            proportion anchors per archetype
|-- scripts/
|   |-- generate_bbmodel.py        deterministic generator + 3 validation layers
|   `-- bbmodel_to_spec.py         reverse a .bbmodel back into an editable spec
`-- examples/
    |-- rabbit.spec.json           quadruped, auto atlas (64x32)
    `-- oak_stool.spec.json        furniture_block, fixed 64x32 atlas
```

## Quick start

```bash
python scripts/generate_bbmodel.py examples/rabbit.spec.json -o rabbit.bbmodel --png rabbit.png
```

Flags:

- `--png PATH` also write the atlas PNG for inspection.
- `--strict` enforce archetype proportion rules as hard errors (off by default; proportions are advisory).
- `--no-texture` geometry-only first pass (no embedded atlas).

On success it prints part count, atlas size, texture count, and any notes. Only
*structural* problems block (and write nothing): missing/duplicate ids, unresolved
`parent`, a face detail bound to a missing part or a UV outside the atlas, non-ASCII
names, missing `size`. Proportion mismatches are advisory and still generate.

## Authoring a spec

Fill `schema/asset_spec.schema.json`. Minimum:

```jsonc
{
  "meta": { "name": "rabbit" },        // ASCII only
  "target": "entity",                   // block | entity | item
  "archetype": "quadruped",             // optional, enables proportion checks
  "atlas": { "width": "auto", "height": "auto" },
  "parts": [
    { "id": "body", "size": [6,5,9], "pos": [-3,4,-4] },
    { "id": "head", "parent": "body", "size": [5,5,5], "pos": [-2.5,7,-9], "pivot": [0,7,-6] },
    { "id": "ear_L", "parent": "head", "size": [1,5,2], "pos": [0.5,11,-7] }
  ],
  "face_details": [
    { "id": "eye_L", "part": "head", "kind": "texture", "face": "north", "uv": [33,2,1,1], "color": "#222" },
    { "id": "ears",  "part": "ear_L", "kind": "geometry" }
  ]
}
```

Rules that carry the design:

- **`size` is explicit pixels.** This is what removes "the AI guessed the proportions".
- **`pos` is the min corner**; `to = pos + size` is computed for you.
- **`parent`** builds the outliner hierarchy and rotation pivots.
- **`face_details`** is the binding checklist as data: `geometry` details must be real
  parts; `texture` details must name a real part + face and sit inside the atlas. Both
  are validated.

## Flexible texture size

`atlas.width` / `atlas.height` accept any integer (16, 32, 64, 128, ...), non-square
sizes (e.g. 64x32), or `"auto"` to pick the smallest power-of-two that fits the box-UV
layout and any declared regions. Textures are embedded in the `.bbmodel`
(`internal: true`, `saved: false`, base64 `source`, no external `path`).

## Proportion anchors (a sanity net, not a style police)

`presets/archetypes.json` holds ratio rules that flag *accidental* deformation. They are
**advisory by default — they never block generation**. Unusual proportions are often
intentional (a long-eared alien, a totem, a bobble-head), so the generator reports the
note and keeps going. Examples for `quadruped`:

- `ear.h <= head.h * 1.4` — antenna-ear nudge.
- `body.w >= body.h * 0.55` — column-body nudge.
- `leg.h <= body.h * 1.5` — stilt-leg nudge.

Pass `--strict` to turn `severity: error` rules into hard failures (for batch-generating
conventional mobs to a house style). Omit `archetype` entirely and no checks run.

Add a new archetype by adding a block with `_roles` (role -> candidate part ids) and
`rules` (an `expr` over role dims `.w/.h/.d` and `bbox`). Absent roles skip their rules.

## Image input / concept images

**Image-to-model is supported.** It runs through the spec, exactly like text:

```text
image -> [AI reads its structure] -> asset spec -> generator -> .bbmodel
```

Reading the image means extracting structure: major volumes, footprint, repeated parts,
which features are geometry vs texture — then writing those as cuboid parts with explicit
sizes. What you must **not** do is trace it pixel-for-pixel: perspective, shadows, and
highlights are lighting, not shape, and copying them is what produces mesh noise. The
image is a structural hint; the spec is the blueprint.

Concept-image *generation* is a separate, optional, reference-only step — handy to
confirm a visual direction before modeling, never an input to the spec or geometry. The
reliable review artifact is the generated `.bbmodel` opened in Blockbench.

## Editing an existing model (round-trip)

To keep iterating on a model you (or someone else) already started — or to rough something
out, generate it, then refine the spec — reverse it back to a spec:

```bash
python scripts/bbmodel_to_spec.py model.bbmodel -o model.spec.json --dump-texture tex.png
```

Then edit `model.spec.json` and re-run `generate_bbmodel.py`. The geometry round-trips
losslessly: `pos`, `size`, `pivot`, `rotation`, `inflate`, the part hierarchy, the UV
layout, and the atlas resolution all come back exactly.

Lossy parts (reported on every run, not silently dropped):

- **Texture pixels** can't be expressed as spec `regions`/`face_details`, so `--dump-texture`
  saves the embedded PNG instead. Re-attach or re-declare details by hand if you need them.
- **`face_details`** (which painted pixels were "eyes" vs "fur") can't be inferred; the
  field comes back empty.
- **`target`** is guessed from the coordinate range, **`archetype`** is left blank.

It also handles foreign `.bbmodel` files (flat outliners, duplicate or CJK element names):
names are sanitized to unique ASCII ids, and faces that don't fit the box-UV layout are
recovered as explicit `per_face` UVs.

## What you get

A clean, editable, correctly-proportioned first pass — a good Blockbench starting point,
not a finished production asset. Open it and polish.

## A note on expectations

Because of current AI limitations and the constraints of the skill itself, a model this
skill produces directly may not fully meet your requirements. Treat the output as an
editable first draft, not a finished asset — open it in Blockbench and refine. I'll keep
updating and improving this skill over time.
