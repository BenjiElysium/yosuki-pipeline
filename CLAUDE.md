# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Yosuki Motion Graphics Pipeline — generates per-variant ad creatives for a Japanese musical instrument brand. The codebase is an early-stage multi-layer pipeline; only the input layer exists so far.

## Environment & commands

- Python 3.13 (pinned via `.python-version`), managed with `uv` (`uv.lock` is the source of truth).
- Install / sync deps: `uv sync`
- Run a script: `uv run python input_layer.py --brief campaign_brief.json --out variant_manifest.json`
- Requires `ANTHROPIC_API_KEY` in `.env` (loaded via `python-dotenv`). `REPLICATE_API_TOKEN` is expected once the generation layer lands (`replicate` is already a dep).
- No test suite, linter, or formatter is configured yet.

## Architecture

The pipeline is designed as three layers that communicate via JSON artifacts on disk. Treat each JSON file as the contract between layers — do not break its shape without updating downstream consumers.

1. **Input layer** (`input_layer.py`, implemented)
   - Reads `campaign_brief.json`, validated against `BRIEF_SCHEMA` (brand, campaign, products with variants and image_paths).
   - For each `(product, color_variant)` pair, calls Claude (`claude-sonnet-4-20250514`) to generate structured copy (`tagline`, `cta`, `creative_direction`), validated against `COPY_RESPONSE_SCHEMA`. One call is shared across all aspect ratios to save tokens.
   - Flattens `products × variants × aspect_ratios` into a flat list of variant records and writes `variant_manifest.json`. `variant_id` format is `{model_id}_{color_variant}_{aspect_ratio}`.
   - Failure modes: one retry on JSON/schema parse failure, then falls back to a safe copy object so the pipeline keeps moving rather than aborting.
   - `bg_image_path` is intentionally left `None` — it's populated by the next layer.

2. **Generation layer** (`generation_layer.py`, not yet in repo)
   - Expected to consume `variant_manifest.json` and populate `bg_image_path` (likely via Replicate given the dep).

3. **Orchestration** (`orchestrate.py`, not yet in repo)
   - Expected to drive the full pipeline end-to-end.

When adding layers, keep the manifest as the single hand-off surface and preserve existing field names (`variant_id`, `product_image_path`, `render_path`, `bg_image_path`, `output_path`, etc.) — `input_layer.py` already writes consumers' expected shape.

## Schema conventions

- `BRIEF_SCHEMA` restricts `product_line` to `guitar | piano | saxophone` and `duration_seconds` to 5–10.
- Copy length caps (tagline 60, cta 40, creative_direction 300) are re-enforced in Python after Claude responds — schema `maxLength` is treated as advisory to the model, not a guarantee.
- Claude's response is expected as raw JSON; the parser tolerates accidental ```` ```json ```` fences but the prompt tells the model not to use them.
