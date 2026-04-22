"""
generation_layer.py — Yosuki Motion Graphics Pipeline
-----------------------------------------------------
Reads variant_manifest.json, generates a background image per unique
(model_id, color_variant, aspect_ratio) combo via Replicate
(black-forest-labs/flux-2-pro) at the correct pixel dimensions for each
aspect ratio, saves PNGs under assets/generated/, and writes bg_image_path
back into each matching variant record in the manifest.

Usage:
    python generation_layer.py [--manifest path/to/manifest.json] [--dry-run]

Requirements:
    pip install replicate python-dotenv pillow
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
GENERATED_DIR = Path("assets/generated")
SLEEP_BETWEEN_CALLS = 2  # seconds

# Replicate input params per aspect-ratio label. Flux ignores width/height
# unless aspect_ratio="custom"; width/height must be multiples of 16 (970→976).
# Delivery layer can crop the extra 6px horizontally on the 970x250 spot.
REPLICATE_PARAMS_BY_RATIO = {
    # All-custom so we own exact dimensions. Values are rounded UP to the nearest
    # multiple of 16 where needed; the delivery layer can center-crop to exact.
    "1920x1080": {"aspect_ratio": "custom", "width": 1920, "height": 1088},  # crop 8h
    "1080x1080": {"aspect_ratio": "custom", "width": 1088, "height": 1088},  # crop 8w, 8h
    "970x250":   {"aspect_ratio": "custom", "width": 976,  "height": 256},   # crop 6w, 6h
}

PROMPT_SUFFIX = " Background scene only. Empty of people and objects. Photorealistic. Cinematic."

ANATOMY_BY_LINE = {
    "guitar": ["fretboard", "strings", "headstock", "neck", "pickguard", "tuning peg", "body"],
    "piano": ["keys", "keyboard", "keybed", "pedals", "lid", "soundboard", "hammers"],
    "saxophone": ["bell", "reed", "mouthpiece", "keys", "neck strap", "pad"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_prompt(text: str, variant: dict) -> str:
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
    # Collapse whitespace and stitch up orphaned punctuation gaps left behind
    result = re.sub(r"\s+", " ", result)
    result = re.sub(r"\s+([.,;:'’])", r"\1", result)
    return result.strip()


def build_prompt(combo: dict) -> str:
    """
    Prefer a structured scene_direction JSON (authored directly for Flux);
    fall back to Claude's summarized creative_direction otherwise. We only
    sanitize the fallback — stripping words from the authored JSON would
    corrupt its structure.
    """
    scene_direction = combo.get("scene_direction") or ""
    try:
        parsed = json.loads(scene_direction)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        return scene_direction.strip() + PROMPT_SUFFIX

    sanitized = sanitize_prompt(combo["creative_direction"], combo)
    return sanitized + PROMPT_SUFFIX


def unique_combos(variants: list[dict]) -> list[dict]:
    """Dedupe variants by (model_id, color_variant, aspect_ratio)."""
    seen: "OrderedDict[tuple[str, str, str], dict]" = OrderedDict()
    for v in variants:
        key = (v["model_id"], v["color_variant"], v["aspect_ratio"])
        if key not in seen:
            seen[key] = v
    return list(seen.values())


def download_output(output, dest: Path) -> None:
    """Write Replicate's output bytes to disk. output_format=png is set on the call."""
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
    parser = argparse.ArgumentParser(description="Yosuki pipeline — Generation Layer")
    parser.add_argument("--manifest", default="variant_manifest.json", help="Path to variant manifest")
    parser.add_argument("--dry-run", action="store_true", help="Print prompts without calling Replicate")
    parser.add_argument("--limit", type=int, default=None,
                        help="Generate only the first N combos. Test mode: does not update the manifest.")
    parser.add_argument("--product-line", default=None,
                        help="Only generate for combos matching this product_line (e.g. 'piano'). "
                             "Test mode: does not update the manifest.")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"✗ Manifest not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    combos = unique_combos(manifest["variants"])
    total_variants = len(manifest["variants"])

    if args.product_line is not None:
        combos = [c for c in combos if c["product_line"] == args.product_line]
        print(f"  [TEST MODE — filtering to product_line='{args.product_line}' "
              f"({len(combos)} combos), manifest will NOT be updated]")

    if args.limit is not None:
        combos = combos[: args.limit]
        print(f"  [TEST MODE — limiting to first {args.limit} combos, manifest will NOT be updated]")

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
        prompt = build_prompt(combo)

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
        print(f"\n✓ Test round complete: {len(combo_to_bg)}/{len(combos)} images generated.")
        print(f"  Manifest NOT modified. Verify dimensions then re-run without filters.")
        return

    # Write bg_image_path back to every variant record whose combo was generated
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
