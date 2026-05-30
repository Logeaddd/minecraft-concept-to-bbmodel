---
name: minecraft-concept-to-bbmodel
description: Convert a user description or reference image into a Minecraft/Blockbench .bbmodel by first authoring a structured asset spec (intermediate JSON), then running a deterministic generator that emits validated cuboid geometry, hierarchy, UVs, and an optional embedded pixel-art texture. The core capability is the spec schema plus the generator script, NOT AI hand-writing .bbmodel JSON and NOT image generation. Use when the user asks to design or make a Minecraft/Blockbench model from text, images, furniture, props, creatures, or an existing .bbmodel.
---

# Minecraft Concept To BBModel

## Core principle

Never hand-write `.bbmodel` JSON, and never derive geometry directly from a picture.
A language model cannot reliably estimate `from`/`to`/`origin`/`uv`; doing so produces
the classic failures: broken proportions (ears too tall, body like a column), float
noise that looks like an auto-reconstructed mesh, Z-fighting, and texture detail that
was painted into the atlas but never bound to the correct cuboid face.

Instead the pipeline is split so the model only does what it is good at (filling in a
structured spec) and a deterministic script does everything that must be exact:

```text
input (text / image)
  -> [AI]    author asset spec JSON   (schema/asset_spec.schema.json)
  -> [script] validate_schema          (structure, ids, ascii, references)
  -> [script] validate_geometry        (archetype proportion anchors)
  -> [script] build .bbmodel           (grid-snapped cuboids, hierarchy, UVs)
  -> [script] render atlas             (Pillow, deterministic, flexible size)
  -> [script] validate_bbmodel         (faces, uv bounds, detail binding, encoding)
  -> .bbmodel  ->  open in Blockbench for manual polish
```

The AI fills the spec. The script `scripts/generate_bbmodel.py` does the rest.
Image generation is optional and only ever a reference/marketing artifact (see the
bottom section); it never feeds geometry.

## Files in this skill

- `schema/asset_spec.schema.json` — the intermediate spec contract the AI fills in.
- `scripts/generate_bbmodel.py` — deterministic generator + 3 validation layers.
- `scripts/bbmodel_to_spec.py` — reverse a `.bbmodel` back into an editable spec (for iterating on existing models).
- `presets/archetypes.json` — proportion anchors per archetype (quadruped, humanoid, furniture_block, item_simple).
- `examples/rabbit.spec.json`, `examples/oak_stool.spec.json` — worked specs.

## Workflow

1. Normalize the input into an asset spec (do NOT model yet).
   - From text: extract object type, target (block/entity/item), main volumes, part
     hierarchy, which details are geometry vs texture, and approximate pixel sizes.
   - From an image: read the silhouette, footprint, repeated parts, and material. Do
     NOT copy perspective, shading, or highlights into geometry. Translate the picture
     into cuboid volumes with explicit pixel sizes. A picture is a hint, not a blueprint.
   - Pick an `archetype` when one fits; it activates proportion checks that catch bad
     designs before they reach Blockbench.

2. Write the spec to a `*.spec.json` file following `schema/asset_spec.schema.json`.
   - Every part needs an explicit `size` (pixels) and `pos` (min corner). Never leave
     sizes to be "estimated" later.
   - Use `parent` to build hierarchy so the generator produces correct outliner groups
     and rotation pivots.
   - Mark each important detail in `face_details` with `kind: texture` or
     `kind: geometry`. Things that protrude (ears, handles, spikes) are `geometry` and
     must be real parts; flat marks (eyes, labels) are `texture` bound to a named face.

3. Run the generator. It validates and builds in one pass:
   ```bash
   python scripts/generate_bbmodel.py path/to/asset.spec.json -o out.bbmodel --png out.png
   ```
   - Add `--strict` to treat proportion warnings as hard errors.
   - Add `--no-texture` to skip the atlas (geometry-only first pass).

4. Read the generator output.
   - If validation FAILS, fix the spec and re-run. Do not patch the `.bbmodel` by hand.
     Hard failures are structural only: duplicate/missing ids, unresolved `parent`,
     face detail bound to a missing part or a UV outside the atlas, non-ASCII names,
     missing `size`. These would produce a broken file, so they block.
   - Proportion notes (e.g. `ear_not_too_tall`, `body_not_column`) are ADVISORY and do
     NOT block. Unusual proportions are often intentional (a long-eared alien, a totem,
     a snowman, a bobble-head). Treat them as a sanity nudge: if the look is deliberate,
     ignore the note; if it was an accident, adjust the size in the spec.
   - Use `--strict` only when you want conventional proportions enforced as hard errors
     (e.g. batch-generating standard mobs to a house style).

5. Report the result and hand off to Blockbench for manual polish. The goal is a clean,
   editable, correctly-proportioned first pass, not a finished production asset.

## The asset spec (what the AI fills in)

Authoritative contract: `schema/asset_spec.schema.json`. Shape:

```jsonc
{
  "meta": { "name": "rabbit" },          // ASCII name, no CJK
  "target": "entity",                     // block | entity | item (locks range checks)
  "archetype": "quadruped",               // optional, activates proportion anchors
  "atlas": {
    "width": "auto", "height": "auto",    // int (16/32/64/128/non-square) OR "auto"
    "regions": [                          // optional base fills
      { "id": "fur", "rect": [0,0,64,32], "fill": "#c9a66b",
        "palette": ["#c9a66b","#bb9659"], "noise": 0.25 }
    ]
  },
  "parts": [
    { "id": "body", "size": [6,5,9], "pos": [-3,4,-4], "uv_origin": [0,0] },
    { "id": "head", "parent": "body", "size": [5,5,5], "pos": [-2.5,7,-9],
      "pivot": [0,7,-6], "uv_origin": [30,0] },
    { "id": "ear_L", "parent": "head", "size": [1,5,2], "pos": [0.5,11,-7],
      "uv_origin": [52,0] }
  ],
  "face_details": [
    { "id": "eye_L", "part": "head", "kind": "texture", "face": "north",
      "uv": [33,2,1,1], "color": "#222222" },
    { "id": "ears", "part": "ear_L", "kind": "geometry" }
  ]
}
```

Field rules that matter:
- `size` = `[w,h,d]` in pixels, explicit. This single rule is what kills "AI guessed
  the proportions". `pos` is the MIN corner; the generator computes `to = pos + size`.
- `parent` builds the hierarchy. The generator nests outliner groups and uses `pivot`
  (defaults to `pos`) as the rotation origin so `rot` behaves.
- `uv_mode` is `box` (default, single `uv_origin`) or `per_face` (explicit `faces` map).
- `atlas.width/height` are flexible: any integer, non-square allowed, or `"auto"` to let
  the generator pick the smallest power-of-two that fits the box-uv layout and regions.
- `face_details` is the binding checklist as DATA. `kind: geometry` details must exist
  as real parts (validated). `kind: texture` details must name a real `part` + `face`
  and sit inside the atlas (validated). This is what stops "painted in the atlas but
  not bound to any face".

## Texture / atlas

- Size is deliberately flexible. Use `"auto"` when you do not care, or pin an exact size
  (16, 32, 64, 128, or non-square like 64x32) when you are laying UVs out by hand.
- `regions` paint deterministic base fills with an optional palette + noise for grain.
- `face_details` of `kind: texture` paint on top at their `uv` rect.
- The generated texture is embedded: `internal: true`, `saved: false`, `source` is a
  `data:image/png;base64,...` URL, and there is NO external `path` (which would break
  when the model moves to another machine). The generator handles this; do not add a
  fake `path`.
- `--png` also writes the atlas as a standalone PNG for visual inspection.

## Proportion anchors (a sanity net, not a style police)

`presets/archetypes.json` defines ratio rules per archetype. By default they are
**advisory**: the geometry validator reports them and keeps generating. They exist to
catch *accidental* deformation (the AI fat-fingered a size), not to forbid unusual
designs. A long-eared alien, a totem, a stylized bobble-head are all legitimate; ignore
the note when the look is intentional. `--strict` promotes `severity: error` rules to
hard failures for callers who deliberately want a conventional house style.

Resolved roles per archetype (e.g. `quadruped`: head, body, ear, leg) feed ratio checks
like:

- `ear.h <= head.h * 1.4` — flags antenna-like ears (advisory).
- `body.w >= body.h * 0.55` — flags column/pillar bodies (advisory).
- `body.d >= body.w * 0.8` — quadruped bodies are usually deeper than wide.
- `leg.h <= body.h * 1.5` — flags stilt legs.
- furniture_block: `bbox.w/d <= 16` — block-footprint guidance.

To support a new creature type, add an archetype block with `_roles` (role -> candidate
part ids) and `rules` (each `expr` is evaluated against role dims `.w/.h/.d` plus
`bbox`). A rule that references an absent role is skipped, so partial models are fine.
Archetype is also entirely optional: omit it and no proportion checks run at all.

## Inspecting / editing an existing .bbmodel

- To iterate on an existing model (yours or someone else's), reverse it back into a spec,
  edit the spec, then regenerate:
  ```bash
  python scripts/bbmodel_to_spec.py model.bbmodel -o model.spec.json --dump-texture tex.png
  # edit model.spec.json, then:
  python scripts/generate_bbmodel.py model.spec.json -o model.bbmodel --png tex.png
  ```
  Geometry round-trips losslessly (pos/size/pivot/rotation/inflate, hierarchy, UV layout,
  resolution). Lossy: texture PIXELS (use `--dump-texture`), `face_details` (returns
  empty — re-declare important ones), and `target`/`archetype` (guessed/blank). The
  reverse script also tolerates foreign models: it sanitizes non-ASCII/duplicate element
  names to unique ASCII ids and recovers non-box UVs as explicit `per_face`.
- For a tiny tweak you may edit the `.bbmodel` directly, but prefer the spec round-trip so
  validation still applies.
- Preserve user-edited geometry unless they ask for a rebuild. Keep `resolution`.
- Keep all internal names ASCII to avoid replacement-character (`?`) corruption. For a
  Chinese-facing filename, build the path with Python Unicode escapes, e.g.
  `"\u5154\u5b50.bbmodel"`, rather than piping Chinese through PowerShell.

## Validation guarantees (run automatically by the generator)

Two tiers:

- HARD (block generation, exit non-zero, nothing written) — only things that make a
  genuinely broken file:
  1. Schema: required fields, unique ASCII ids, resolvable `parent`, valid `face_details`.
  2. BBModel: every face texture index exists, every `face_details` UV falls inside the
     atlas, every detail maps to a real part/face, no replacement chars anywhere.
- ADVISORY (reported, never block unless `--strict`):
  3. Geometry: archetype proportion notes.

Fix the spec for hard failures. Weigh advisory notes against your intent.

## Image input IS supported — through the spec, not by tracing

When the user supplies a reference image, the path is fully available and is the same
pipeline as text:

```text
image -> [AI reads it] -> asset spec -> generator -> .bbmodel
```

Reading the image means understanding its STRUCTURE, then expressing that as cuboid
parts with explicit sizes:

- Identify the major volumes, footprint, height, repeated parts (legs, ears, drawers).
- Decide which features are geometry (protruding: ears, handles, spikes) and which are
  texture (flat marks: eyes, labels, panels).
- Approximate each volume's pixel size and position, and write them into `parts`.
- Translate colors/material into atlas regions and `face_details`.

What you must NOT do is trace the image pixel-for-pixel into geometry. Perspective,
shading, ambient occlusion, and edge highlights are lighting, not shape — copying them
is exactly what produces mesh noise and phantom blocks. The image is a structural hint;
the spec is the blueprint. This indirection is the whole point and it is what makes
image input reliable rather than a coin flip.

## Optional: generated concept image (reference only, never geometry)

Separately, you may GENERATE a concept image when the user wants to confirm a visual
direction before modeling. Treat it strictly as reference/promo art — it never feeds the
spec or geometry. Prefer showing the actual generated `.bbmodel` / Blockbench preview,
which is the reliable review artifact. If you do generate one, keep the prompt
Minecraft/Blockbench-oriented (cuboid construction, pixel-art material, orthographic
top/front/side, one-block scale reference, no smooth realistic mesh) — but never try to
trace geometry back out of the picture.

## Implementation notes

- Python 3 + Pillow for the deterministic atlas. The generator degrades gracefully if
  Pillow is missing (geometry still builds; pass `--no-texture` or accept no embedded
  texture).
- Grid-snap is 0.5px (`GRID` in the script). This removes float noise; do not defeat it
  with arbitrary fractional sizes unless you mean it.
- Anti-Z-fighting offsets are applied only to parts tagged `z_offset_tag: true`, not
  sprinkled randomly.

<!-- __CONTINUE_HERE__ -->

<!-- __CONTINUE_HERE__ -->
