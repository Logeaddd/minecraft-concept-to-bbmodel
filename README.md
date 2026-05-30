# minecraft-concept-to-bbmodel

Convert text descriptions or reference images into editable Blockbench `.bbmodel` files through a structured asset spec and a deterministic generator.

This is not a one-shot AI mesh generator. Codex writes a clear intermediate spec with explicit cuboid sizes, positions, hierarchy, pivots, UVs, and texture details; the bundled Python script validates that spec and builds the `.bbmodel`.

The goal is a clean editable first draft for Blockbench, not a finished production asset.

## What It Does

- Turns text concepts into Minecraft / Blockbench-style cuboid models.
- Uses reference images as structural hints, not pixel-traced geometry.
- Generates editable `.bbmodel` files.
- Can generate an embedded texture atlas and optional PNG preview.
- Validates part ids, parents, UV bounds, texture bindings, and non-ASCII ids.
- Provides advisory proportion checks for model archetypes.
- Can reverse an existing `.bbmodel` back into an editable spec.

## How It Works

```text
input text or image
  -> Codex authors asset spec JSON
  -> script validates schema
  -> script validates geometry
  -> script builds .bbmodel
  -> script renders optional texture atlas
  -> script validates the .bbmodel
  -> open in Blockbench for polish
```

The important design choice is that Codex should not hand-write `.bbmodel` JSON directly. The `*.spec.json` file is the editable source of truth, and `scripts/generate_bbmodel.py` produces the final Blockbench file.

## Install

Clone this repository:

```bash
git clone https://github.com/Logeaddd/minecraft-concept-to-bbmodel.git
```

Copy the skill folder into your Codex skills directory.

macOS / Linux:

```bash
mkdir -p ~/.codex/skills
cp -r minecraft-concept-to-bbmodel/minecraft-concept-to-bbmodel ~/.codex/skills/
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills"
Copy-Item -Recurse ".\minecraft-concept-to-bbmodel\minecraft-concept-to-bbmodel" "$env:USERPROFILE\.codex\skills\"
```

Restart Codex so it can discover the skill.

## Usage Through Codex

Ask Codex to use the skill:

```text
Use minecraft-concept-to-bbmodel to make a cute plush bunny chair for Blockbench.
```

or:

```text
Use minecraft-concept-to-bbmodel to create an original transforming robot .bbmodel.
```

Codex should create a `*.spec.json` file first, then run the generator:

```bash
python scripts/generate_bbmodel.py path/to/model.spec.json -o model.bbmodel --png model_atlas.png
```

## Manual Generator Usage

From inside the skill folder:

```bash
python scripts/generate_bbmodel.py path/to/model.spec.json -o model.bbmodel --png model_atlas.png
```

Useful flags:

- `--png PATH` writes the generated texture atlas as a PNG.
- `--strict` turns proportion warnings into hard failures.
- `--no-texture` builds geometry only.

On success, the generator prints the part count, atlas size, texture count, and any notes. Structural problems block generation: duplicate ids, unresolved parents, invalid face detail references, UVs outside the atlas, non-ASCII ids, or missing sizes. Proportion mismatches are advisory unless `--strict` is used.

## Repository Layout

```text
minecraft-concept-to-bbmodel/
  LICENSE
  README.md
  minecraft-concept-to-bbmodel/
    SKILL.md
    schema/
      asset_spec.schema.json
    presets/
      archetypes.json
    scripts/
      generate_bbmodel.py
      bbmodel_to_spec.py
```

The inner `minecraft-concept-to-bbmodel/` folder is the actual Codex skill folder.

## Requirements

- Python 3
- Pillow is recommended for texture atlas generation:

```bash
pip install pillow
```

If Pillow is not installed, geometry generation can still work, but texture output may be limited.

## Asset Spec

The generator consumes a structured JSON spec. A minimal spec looks like this:

```json
{
  "meta": { "name": "rabbit" },
  "target": "entity",
  "archetype": "quadruped",
  "atlas": { "width": "auto", "height": "auto" },
  "parts": [
    { "id": "body", "size": [6, 5, 9], "pos": [-3, 4, -4] },
    { "id": "head", "parent": "body", "size": [5, 5, 5], "pos": [-2.5, 7, -9], "pivot": [0, 7, -6] },
    { "id": "ear_L", "parent": "head", "size": [1, 5, 2], "pos": [0.5, 11, -7] }
  ],
  "face_details": [
    { "id": "eye_L", "part": "head", "kind": "texture", "face": "north", "uv": [33, 2, 1, 1], "color": "#222222" },
    { "id": "ear_geometry", "part": "ear_L", "kind": "geometry" }
  ]
}
```

Important rules:

- `size` is explicit pixels.
- `pos` is the minimum corner.
- `parent` builds the Blockbench outliner hierarchy.
- `pivot` controls rotation origin.
- `face_details` records whether a detail is real geometry or texture.
- Geometry details must exist as parts.
- Texture details must bind to a real part and face.

## Image Input

Image input is supported through the same spec workflow:

```text
image -> Codex reads structure -> asset spec -> generator -> .bbmodel
```

The image should be treated as a structural reference: major volumes, footprint, repeated parts, protrusions, and material cues. Do not trace perspective, shadows, highlights, or pixel noise into geometry.

## Editing Existing Models

Convert an existing `.bbmodel` back into a spec:

```bash
python scripts/bbmodel_to_spec.py model.bbmodel -o model.spec.json --dump-texture tex.png
```

Then edit the spec and regenerate. Geometry round-trips include position, size, pivot, rotation, inflate, hierarchy, UV layout, and atlas resolution. Texture pixels and semantic `face_details` cannot be fully inferred, so re-declare important texture details manually when needed.

## Expectations

This skill produces a clean, editable Blockbench starting point. Open the generated `.bbmodel` in Blockbench and polish proportions, pivots, textures, and animations manually.
