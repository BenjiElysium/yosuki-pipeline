# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Yosuki Motion Graphics Pipeline — generates per-variant ad creatives (MP4s) for a Japanese musical instrument brand. Three layers communicate via JSON artifacts on disk; Claude writes copy, Flux generates backgrounds, After Effects renders the final video. README.md covers install (incl. one-time `sudo` nexrender patch) and end-to-end usage; this file covers the architecture invariants you need before changing code.

## Environment & commands

- Python 3.13 (pinned via `.python-version`), managed with `uv` (`uv.lock` is the source of truth). Install/sync: `uv sync`.
- Required env (`.env`, loaded via `python-dotenv`): `ANTHROPIC_API_KEY`, `REPLICATE_API_TOKEN`, `AERENDER_PATH` (quote it — the macOS default contains spaces).
- Run a layer: `uv run python <layer>.py [flags]`. Common flags: `--dry-run`, `--limit N`, `--product-line guitar`, `--filter <substr>` (orchestrate, repeatable AND).
- No test suite, linter, or formatter is configured.

## Architecture

Pipeline is three layers. **The single hand-off surface between them is `variant_manifest.json`** — do not break field names without updating downstream consumers. Layers run independently; you can re-run any one without redoing earlier ones (generation reads `flux_prompt` from the manifest, orchestrate reads `bg_image_path`).

1. **Input layer** (`input_layer.py`) — reads `campaign_brief.json`, validates against `BRIEF_SCHEMA`, calls Claude (`claude-sonnet-4-6`) once per `(product, color_variant)` pair (one call shared across all aspect ratios), validates the response against `COPY_RESPONSE_SCHEMA` (which embeds a structured `flux_prompt` object), flattens `products × variants × aspect_ratios` into a flat list of variant records, and writes the manifest with `schema_version: 2`.
2. **Generation layer** (`generation_layer.py`) — reads the manifest, deduplicates by `(model_id, color_variant, aspect_ratio)` to avoid paying for identical Flux calls across variants that share a background, serializes each `flux_prompt` to a JSON string and calls Replicate (`black-forest-labs/flux-2-pro`), saves PNGs under `assets/generated/`, and writes `bg_image_path` back into every matching variant record.
3. **Orchestration** (`orchestrate.py`) — reads the manifest, emits a nexrender job JSON per variant to `jobs/`, and shells out to `nexrender-cli` (+ `aerender`) to render MP4s to `output/`.

The brief's `scene_direction` field is a plain-English mood seed for Claude — not a Flux prompt. Claude authors the structured `flux_prompt` itself, per variant, and that's what reaches Replicate. `generation_layer.py` enforces `schema_version == 2` on the manifest.

## Schema & copy conventions

- `BRIEF_SCHEMA` restricts `product_line` to `guitar | piano | saxophone` and `duration_seconds` to 5–10. Aspect ratios are free strings but only `1920x1080`, `1080x1080`, `970x250` are wired through to AE comps and Replicate dimensions.
- Schema `maxLength` is treated as advisory to Claude. Python re-truncates after the response in `apply_truncation()` — when adjusting caps (tagline 40, cta 25, creative_direction 300, each flux_prompt string field 150), change both the prompt text and the truncation constants.
- Claude's response is expected as raw JSON; the parser tolerates accidental ```` ```json ```` fences but the prompt forbids them.
- One retry on JSON/schema parse failure, then `safe_fallback()` returns a minimal valid copy object so the pipeline keeps moving rather than aborting on a single bad call.

## Generation layer invariants

- Replicate's flux-2-pro ignores `width`/`height` unless `aspect_ratio="custom"` is set, and dimensions must be multiples of 16. We use 976×256 / 1920×1088 / 1088×1088; AE center-crops the 6–8px slack. See `REPLICATE_PARAMS_BY_RATIO`.
- The prompt is sanitized against product-line anatomy words (`ANATOMY_BY_LINE`) plus `model_name`/`model_id`/`product_line` before calling Flux — a safety net to keep the model from hallucinating the instrument into the background. Don't remove it; extend the list if you add a product line.
- `--limit`, `--product-line`, `--model-id` cause **partial** manifest updates (only matching variants get `bg_image_path` written). Don't assume a manifest is fully populated after a filtered run.

## Orchestration layer invariants

- AE comp names are looked up by aspect ratio in `COMP_BY_RATIO` (`yosuki_billboard` / `yosuki_16x9` / `yosuki_1x1`), all in `templates/yosuki_templates.aep`. Adding an aspect ratio means adding a comp to the .aep AND an entry here AND a `REPLICATE_PARAMS_BY_RATIO` entry.
- Every `file://` asset in the emitted job JSON sets `useOriginal: true`. This is a workaround for a nexrender bug (`@nexrender/core` `download.js:163-166`) that mangles deep absolute paths under `/tmp/nexrender/`. If you hand-write a job, preserve this flag on every asset including the template.
- Renders require: (a) the one-time `sudo nexrender-cli` patch step (see README), and (b) AE Preferences → Scripting & Expressions → "Allow Scripts to Write Files and Access Network" enabled. Without (b), `aerender` hangs silently.
- `render_job` has a 900s subprocess timeout per variant.

## Manifest field reference (canonical, do not rename)

Per-variant: `variant_id` (`{model_id}_{color_variant}_{aspect_ratio}`), `product_line`, `model_id`, `model_name`, `color_variant`, `aspect_ratio`, `product_image_path`, `render_path`, `bg_image_path` (None until generation layer), `tagline`, `cta`, `creative_direction`, `flux_prompt`, `scene_direction_seed`, `logo_path`, `primary_color`, `duration_seconds`, `output_path`.
