"""
generation_layer_v2.py — Yosuki Motion Graphics Pipeline (V2)
-------------------------------------------------------------
V2 changes vs generation_layer.py:

    • Reads `flux_prompt` (structured JSON object) directly from each variant
      record in variant_manifest-V2.json — no longer pulls scene_direction
      from the brief.
    • Serializes flux_prompt to a JSON string and sends it to flux-2-pro as
      the prompt (flux-2-pro handles structured JSON prompts well).
    • Writes backgrounds to assets/generated-V2/ (v1 output in assets/generated/
      is preserved).
    • Sanitizes each flux_prompt string field against product/anatomy words
      as a safety net in case Claude slips a disallowed word in.

Usage:
    python generation_layer_v2.py [--manifest variant_manifest-V2.json]
                                  [--dry-run] [--limit N]
                                  [--product-line guitar|piano|saxophone]

Requirements:
    REPLICATE_API_TOKEN must be set in .env or the environment.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path

import replicate
from dotenv import load_dotenv

load_dotenv()

MODEL = "black-forest-labs/flux-2-pro"
GENERATED_DIR = Path("assets/generated-V2")
SLEEP_BETWEEN_CALLS = 2  # seconds

REPLICATE_PARAMS_BY_RATIO = {
    "1920x1080": {"aspect_ratio": "custom", "width": 1920, "height": 1088},
    "1080x1080": {"aspect_ratio": "custom", "width": 1088, "height": 1088},
    "970x250":   {"aspect_ratio": "custom", "width": 976,  "height": 256},
}

PROMPT_SUFFIX = " Background scene only. Empty of people and objects. Photorealistic. Cinematic."

ANATOMY_BY_LINE = {
    "guitar": ["fretboard", "strings", "headstock", "neck", "pickguard", "tuning peg", "body"],
    "piano": ["keys", "keyboard", "keybed", "pedals", "lid", "soundboard", "hammers"],
    "saxophone": ["bell", "reed", "mouthpiece", "keys", "neck strap", "pad"],
}

FLUX_STRING_FIELDS = ("scene", "style", "lighting", "mood", "background", "composition")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_text(text: str, variant: dict) -> str:
    """Strip product identifiers so Flux doesn't hallucinate the instrument."""
    words_to_strip = [
        variant["model_name"],
        *variant["model_name"].split(),
        variant["model_id"],
        variant["product_line"],
        *ANATOMY_BY_LINE.get(variant["product_line"], []),
    ]
    result = text
    for w in words_to_strip:
        result = re.sub(re.escape(w), "", result, flags=re.IGNORECASE)
    result = re.sub(r"\s+", " ", result)
    result = re.sub(r"\s+([.,;:'’])", r"\1", result)
    return result.strip()


def sanitize_flux_prompt(flux_prompt: dict, variant: dict) -> dict:
    """Return a copy of flux_prompt with product/anatomy words stripped from string fields."""
    cleaned = json.loads(json.dumps(flux_prompt))  # deep copy
    for field in FLUX_STRING_FIELDS:
        if field in cleaned and isinstance(cleaned[field], str):
            cleaned[field] = sanitize_text(cleaned[field], variant)
    cam = cleaned.get("camera", {})
    for k in ("angle", "lens", "depth_of_field"):
        if k in cam and isinstance(cam[k], str):
            cam[k] = sanitize_text(cam[k], variant)
    return cleaned


def build_prompt(combo: dict) -> str:
    """
    V2: flux_prompt is a structured JSON object authored by Claude. We
    sanitize its string fields then serialize to JSON for flux-2-pro.
    """
    flux_prompt = combo.get("flux_prompt")
    if not isinstance(flux_prompt, dict):
        raise ValueError(
            f"Variant {combo.get('variant_id')} missing flux_prompt object. "
            f"Did you run input_layer_v2.py against campaign_brief-V2.json?"
        )

    cleaned = sanitize_flux_prompt(flux_prompt, combo)
    return json.dumps(cleaned) + PROMPT_SUFFIX


def unique_combos(variants: list[dict]) -> list[dict]:
    """Dedupe variants by (model_id, color_variant, aspect_ratio)."""
    seen: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()
    for v in variants:
        key = (v["model_id"], v["color_variant"], v["aspect_ratio"])
        if key not in seen:
            seen[key] = v
    return list(seen.values())


def download_output(output, dest: Path) -> None:
    if isinstance(output, list):
        output = output[0]
    if hasattr(output, "read"):
        dest.write_bytes(output.read())
    else:
        with urllib.request.urlopen(str(output)) as resp:
            dest.write_bytes(resp.read())


def generate_background(prompt: str, dest: Path, ratio_params: dict) -> bool:
    input_payload = {"prompt": prompt, "output_format": "png", **ratio_params}
    try:
        output = replicate.run(MODEL, input=input_payload)
        download_output(output, dest)
        return True
    except Exception as e:
        print(f"    ✗ Replicate call failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Yosuki pipeline — Generation Layer V2")
    parser.add_argument("--manifest", default="variant_manifest-V2.json", help="Path to V2 variant manifest")
    parser.add_argument("--dry-run", action="store_true", help="Print prompts without calling Replicate")
    parser.add_argument("--limit", type=int, default=None,
                        help="Generate only the first N combos. Manifest is partially updated "
                             "(only matching variants get bg_image_path written).")
    parser.add_argument("--product-line", default=None,
                        help="Only generate for combos matching this product_line (e.g. 'guitar'). "
                             "Manifest is partially updated (only matching variants get bg_image_path written).")
    parser.add_argument("--model-id", default=None,
                        help="Only generate for a specific model_id (e.g. 'guitar-3'). "
                             "Manifest is partially updated (only matching variants get bg_image_path written).")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"✗ Manifest not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    if manifest.get("schema_version") != 2:
        print(f"⚠  Expected schema_version=2, got {manifest.get('schema_version')}. "
              f"Use generation_layer.py for v1 manifests.")
        sys.exit(1)

    combos = unique_combos(manifest["variants"])
    total_variants = len(manifest["variants"])

    if args.product_line is not None:
        combos = [c for c in combos if c["product_line"] == args.product_line]
        print(f"  [filtering to product_line='{args.product_line}' ({len(combos)} combos); "
              f"manifest will be partially updated]")

    if args.model_id is not None:
        combos = [c for c in combos if c["model_id"] == args.model_id]
        print(f"  [filtering to model_id='{args.model_id}' ({len(combos)} combos); "
              f"manifest will be partially updated]")

    if args.limit is not None:
        combos = combos[: args.limit]
        print(f"  [limiting to first {args.limit} combos; manifest will be partially updated]")

    if not args.dry_run:
        if not os.environ.get("REPLICATE_API_TOKEN"):
            print("✗ REPLICATE_API_TOKEN not set. Add it to .env or export it.")
            sys.exit(1)
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n→ Generating backgrounds for {len(combos)} unique combos "
          f"(applied to {total_variants} variant records)...")
    print("  [DRY RUN — no Replicate calls]\n" if args.dry_run else "")

    combo_to_bg: dict[tuple[str, str, str], str] = {}

    for i, combo in enumerate(combos, start=1):
        aspect_ratio = combo["aspect_ratio"]
        key = (combo["model_id"], combo["color_variant"], aspect_ratio)
        bg_filename = f"{combo['model_id']}_{combo['color_variant']}_{aspect_ratio}_bg.png"
        bg_path = GENERATED_DIR / bg_filename

        try:
            prompt = build_prompt(combo)
        except ValueError as e:
            print(f"  [{i}/{len(combos)}] ✗ {e}")
            continue

        ratio_params = REPLICATE_PARAMS_BY_RATIO.get(aspect_ratio)
        if ratio_params is None:
            print(f"  [{i}/{len(combos)}] ✗ No REPLICATE_PARAMS entry for aspect_ratio '{aspect_ratio}' — skipping")
            continue

        print(f"  [{i}/{len(combos)}] {combo['model_id']} / {combo['color_variant']} / {aspect_ratio} "
              f"(params: {ratio_params})")

        if args.dry_run:
            print(f"    → would save: {bg_path}")
            print(f"    prompt: {prompt}")
            combo_to_bg[key] = str(bg_path)
            continue

        if generate_background(prompt, bg_path, ratio_params):
            print(f"    ✓ saved: {bg_path}")
            combo_to_bg[key] = str(bg_path)

        if i < len(combos):
            time.sleep(SLEEP_BETWEEN_CALLS)

    if args.dry_run:
        print(f"\n  [DRY RUN] Would update {total_variants} variant records with bg_image_path.")
        return

    if args.limit is not None or args.product_line is not None:
        print(f"\n→ Test round complete: {len(combo_to_bg)}/{len(combos)} images generated.")
        print(f"  Writing bg_image_path into matching variants only (other variants untouched).")
        updated = 0
        for v in manifest["variants"]:
            key = (v["model_id"], v["color_variant"], v["aspect_ratio"])
            if key in combo_to_bg:
                v["bg_image_path"] = combo_to_bg[key]
                updated += 1
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"✓ Manifest partially updated: {manifest_path} ({updated} variant records)")
        return

    updated = 0
    for v in manifest["variants"]:
        key = (v["model_id"], v["color_variant"], v["aspect_ratio"])
        if key in combo_to_bg:
            v["bg_image_path"] = combo_to_bg[key]
            updated += 1

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n✓ Manifest updated: {manifest_path}")
    print(f"  Backgrounds generated: {len(combo_to_bg)}/{len(combos)}")
    print(f"  Variant records updated: {updated}/{total_variants}")
    print(f"  Next step: python orchestrate.py --manifest {manifest_path}")


if __name__ == "__main__":
    main()
