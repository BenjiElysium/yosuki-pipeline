# Yosuki Motion Graphics Pipeline

Generate per-variant motion graphics ad creatives for Yosuki Musical Instrument Corporation from a single `campaign_brief.json`. Claude writes the copy, Flux generates the backgrounds, After Effects renders the final MP4s.

## What it does

Three layers communicate via JSON artifacts on disk:

1. **Input layer** (`input_layer.py`) — reads the brief, calls Claude (Sonnet 4.6) to generate tagline / CTA / creative direction + a structured Flux prompt per product-variant, writes `variant_manifest.json`.
2. **Generation layer** (`generation_layer.py`) — reads the manifest, calls Replicate (flux-2-pro) to generate a background image per unique (model × color × aspect_ratio) combo at the correct pixel dimensions, writes PNGs to `assets/generated/<slug>/` and populates `bg_image_path` in the manifest.
3. **Orchestration layer** (`orchestrate.py`) — reads the manifest, emits a nexrender job JSON per variant to `jobs/<slug>/`, and shells out to `nexrender-cli` (+ `aerender`) to render the final MP4s to `output/<slug>/`.

The brief defines a `campaign.slug` (kebab-case, e.g. `2026-launch-yosuki`) — every output path is namespaced under it, so multiple campaigns coexist without collision. The brief also names its template AEP and the comp-per-aspect-ratio mapping, so swapping templates is a brief edit, not a code change.

## Prerequisites

- **macOS** (the pipeline currently targets Adobe After Effects on macOS)
- **Python 3.13** — pinned via `.python-version`, managed with `uv`
- **Node.js 20+** — for `nexrender-cli`
- **Adobe After Effects 2025** — the `aerender` binary ships with it; expected at `/Applications/Adobe After Effects 2025/aerender`
- **Anthropic API key** — for Claude copy generation
- **Replicate API token** — for Flux background generation

## Install

### 1. Clone and install Python deps

```bash
git clone https://github.com/BenjiElysium/yosuki-pipeline.git
cd yosuki-pipeline
uv sync
```

### 2. Create `.env` with API keys

```bash
cat > .env <<EOF
ANTHROPIC_API_KEY=sk-ant-...
REPLICATE_API_TOKEN=r8_...
AERENDER_PATH="/Applications/Adobe After Effects 2025/aerender"
EOF
```

> **Important:** quote `AERENDER_PATH` if it contains spaces (the macOS default path does). Unquoted spaces will break POSIX `source` and confuse argument parsing.

### 3. Install `nexrender-cli` globally

```bash
npm install -g @nexrender/cli @nexrender/action-encode
```

Verify:

```bash
nexrender-cli --version    # → 1.63.3 or later
```

### 4. One-time: authorize `aerender` command-line rendering

This step requires your **system password** and must be done exactly once per machine.

nexrender needs to patch After Effects' `commandLineRenderer.jsx` so scripts can drive renders from the CLI. The patch lives inside `/Applications/Adobe After Effects 2025/Scripts/`, which is owned by `root` — so the patching step requires `sudo`.

From the repo root, run nexrender once with any job JSON (run `uv run python orchestrate.py --jobs-only` first if the `jobs/` directory is empty):

```bash
sudo nexrender-cli \
  -f jobs/guitar-1_midnight-black_1920x1080.json \
  -b "/Applications/Adobe After Effects 2025/aerender"
```

You should see:

```
checking After Effects command line renderer patch...
backing up original command line script to:
 - /Applications/Adobe After Effects 2025/Backup.Scripts/Startup/commandLineRenderer.jsx
patching the command line script
```

After this one-time install, all future renders can run without `sudo`.

### 5. One-time: enable scripting in After Effects

Open After Effects → **Preferences → Scripting & Expressions**:

- ✅ **Allow Scripts to Write Files and Access Network**

Without this, `aerender` will hang or fail silently when the nexrender scripts try to swap layer contents.

### 6. Verify end-to-end

```bash
uv run python input_layer.py                               # generates variant_manifest.json
uv run python generation_layer.py --limit 1                # single-combo smoke test
uv run python orchestrate.py --filter guitar-1 --filter 1920x1080    # single render
```

If an `.mp4` lands under `output/<slug>/`, you're wired up.

## Usage

Run the layers in sequence:

```bash
# 1. Copy generation (one Claude call per product-variant pair)
uv run python input_layer.py --brief campaign_brief.json --out variant_manifest.json

# 2. Background generation (one Replicate call per unique combo)
uv run python generation_layer.py --manifest variant_manifest.json

# 3. Render (one aerender per variant)
uv run python orchestrate.py --manifest variant_manifest.json
```

### Useful flags

**`generation_layer.py`**
- `--dry-run` — print prompts without calling Replicate
- `--limit N` — generate only the first N combos (test mode; does not update the manifest)
- `--product-line piano` — regenerate a single product line after brief edits

**`orchestrate.py`**
- `--filter <substring>` — filter variants by substring match on `variant_id`. Repeatable; all must match (AND).
  Example: `--filter guitar --filter 1920x1080` runs only 1920×1080 guitar variants.
- `--jobs-only` — emit the job JSONs but skip rendering
- `--manifest <path>` — use a different manifest

## Architecture notes

- The **single hand-off surface** between layers is `variant_manifest.json` (currently `schema_version: 3`). If you add a layer, don't break field names (`variant_id`, `product_image_path`, `bg_image_path`, `output_path`, etc.).
- `variant_id` is `{model_id}_{color_variant}_{aspect_ratio}` — unique per output file.
- `generation_layer.py` deduplicates by `(model_id, color_variant, aspect_ratio)` to avoid paying for identical Flux calls across variants that share a background.
- `scene_direction` in the brief is a plain-English mood seed for Claude. Claude itself authors the structured `flux_prompt` JSON object that gets sent to Flux.
- Replicate's flux-2-pro expects `aspect_ratio="custom"` when supplying `width`/`height`; dimensions must be multiples of 16. `flux_dims_for_ratio()` parses the brief's `WxH` string and ceils each axis (e.g. 970→976, 1080→1088); AE center-crops the 6–8px slack.
- `orchestrate.py` emits nexrender jobs with `useOriginal: true` on every `file://` asset — this bypasses a nexrender bug where local files get copied to a malformed path under `/tmp/nexrender/`.

## Aspect ratios and AE comps

| Aspect ratio | Comp name          | Flux dimensions |
|--------------|--------------------|-----------------|
| 970×250      | `yosuki_billboard` | 976×256         |
| 1920×1080    | `yosuki_16x9`      | 1920×1088       |
| 1080×1080    | `yosuki_1x1`       | 1088×1088       |

All comps live in `templates/yosuki_templates.aep`. A `.mogrt` of the 16×9 comp (`templates/yosuki_16x9.mogrt`) ships alongside for Premiere Pro reuse.

## Troubleshooting

**`nexrender exit 2: you might need to try to run nexrender with "sudo" only ONE TIME to install the patch`**
You skipped the one-time sudo patch step. See [Install step 4](#4-one-time-authorize-aerender-command-line-rendering).

**`.env:N: command not found: After`**
`AERENDER_PATH` in `.env` contains spaces but isn't quoted. Wrap it in double quotes.

**`ENOENT: no such file or directory, open '/tmp/nexrender/.../Users/...'`**
The `useOriginal: true` flag is missing from the job's template or assets. `orchestrate.py` sets it automatically; if you hand-write a job JSON, add it to every `file://` asset.

**Render shows the wrong image but the right text**
The `image_paths` mapping in `campaign_brief.json` may not match what's actually in the file. Open the referenced PNG to verify.

**Flux returns the wrong aspect ratio**
flux-2-pro ignores `width`/`height` unless `aspect_ratio` is explicitly set to `"custom"`. See `flux_dims_for_ratio()` in `generation_layer.py`.

## Layout

```
campaign_brief.json        # master input (one per campaign; convention: campaign_brief_<slug>.json)
variant_manifest.json      # hand-off between layers (generated; carries slug + template config)
input_layer.py             # layer 1: Claude → copy + flux_prompt
generation_layer.py        # layer 2: Replicate → backgrounds
orchestrate.py             # layer 3: nexrender → MP4s
assets/                    # product PNGs, logo (tracked)
assets/generated/<slug>/   # Flux backgrounds (gitignored, regenerable)
templates/                 # .aep + .mogrt (tracked)
jobs/<slug>/               # nexrender job JSONs (gitignored)
output/<slug>/             # rendered MP4s (gitignored)
```
