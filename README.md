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

From the repo root, run nexrender once with any job JSON. If `jobs/<slug>/` is empty, generate one first with `uv run python orchestrate.py --jobs-only --filter <variant_id>`:

```bash
sudo nexrender-cli \
  -f jobs/<slug>/<variant_id>.json \
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

**`input_layer.py`**
- `--reuse-bgs-from <slug>` — point each variant's `bg_image_path` at `assets/generated/<slug>/` instead of leaving it `null`. Lets you skip the generation layer when running another language version (or any campaign that shares visuals with an existing one).

**`generation_layer.py`**
- `--dry-run` — print prompts without calling Replicate
- `--limit N` — generate only the first N combos. Manifest is partially updated: only the matching variants get `bg_image_path` written; the rest stay `null`.
- `--product-line piano` — regenerate a single product line after brief edits

**`orchestrate.py`**
- `--filter <substring>` — filter variants by substring match on `variant_id`. Repeatable; all must match (AND).
  Example: `--filter guitar --filter 1920x1080` runs only 1920×1080 guitar variants.
- `--jobs-only` — emit the job JSONs but skip rendering
- `--manifest <path>` — use a different manifest

## Running another language version (e.g. French)

The pipeline has no language switch — Claude infers language from the prompt context, which means you steer it through the brief's free-text fields. The slug-driven paths make a multi-language run safe: outputs land under a different `<slug>/` so nothing collides with the source campaign.

> **Tip — reuse backgrounds.** If the visuals don't need to differ between languages (most common, since the backgrounds are abstract atmospheric scenes), pass `--reuse-bgs-from <source-slug>` to `input_layer.py` and skip the generation layer entirely (~$0.84 saved on a full run). Each variant's `bg_image_path` will point at the source campaign's PNGs. See the alternate commands inline below.

### 1. Duplicate the brief

```bash
cp campaign_brief.json campaign_brief_fr.json
```

### 2. Edit `campaign_brief_fr.json`

Two required edits, one optional:

- **`campaign.slug`** → change to e.g. `2026-launch-yosuki-fr` (kebab-case, must be unique). This is what isolates the new campaign's outputs from the source.
- **`brand.tone`** → append a language directive so Claude writes copy in French. Example:
  ```json
  "tone": "premium, precise, emotive — write all copy in French (FR-FR), no English words"
  ```
  The `tone` field is interpolated into the Claude prompt verbatim, so be specific. Adding "no English words" prevents stray English in CTAs like "Discover".
- *(Optional but recommended)* Rewrite each product's `tagline_hint` and `scene_direction` in French. Native-language seeds give Claude a stronger anchor than instructions alone — best results when both signals point the same way.

### 3. Smoke-test with `--limit 1`

```bash
# ~10 Claude calls — generates French copy + flux_prompts
uv run python input_layer.py --brief campaign_brief_fr.json --out variant_manifest_fr.json

# 1 Replicate call (~$0.04) — generates one background to verify the new manifest works
uv run python generation_layer.py --manifest variant_manifest_fr.json --limit 3

# 1 render — verify the MP4 has French tagline/CTA
uv run python orchestrate.py --manifest variant_manifest_fr.json \
  --filter guitar-1 --filter midnight-black --filter 970x250
```

**Reuse-backgrounds variant.** Replace the first two commands above with a single call:

```bash
uv run python input_layer.py \
  --brief campaign_brief_fr.json \
  --out variant_manifest_fr.json \
  --reuse-bgs-from 2026-launch-yosuki
```

The flag points each variant's `bg_image_path` at `assets/generated/2026-launch-yosuki/` and reports any expected file that's missing (those variants will be skipped at render). Then jump straight to the orchestrate command.

Open the MP4 in `output/2026-launch-yosuki-fr/` and confirm the tagline and CTA are in French. If English slipped through, sharpen the `tone` directive (e.g. add "All output strings — tagline, cta, creative_direction — must be in French.") and re-run step 3 from the top.

### 4. Full run

```bash
# 21 unique backgrounds (~$0.84 at flux-2-pro pricing)
uv run python generation_layer.py --manifest variant_manifest_fr.json

# 30 renders
uv run python orchestrate.py --manifest variant_manifest_fr.json
```

If you used `--reuse-bgs-from` in step 3, skip `generation_layer.py` here — the manifest already points at the source campaign's backgrounds. Just run orchestrate.

### 5. Cleanup

```bash
rm -rf assets/generated/2026-launch-yosuki-fr \
       output/2026-launch-yosuki-fr \
       jobs/2026-launch-yosuki-fr \
       variant_manifest_fr.json
# optionally also delete the brief copy
rm campaign_brief_fr.json
```

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
