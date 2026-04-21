"""
generation_layer.py — Yosuki Motion Graphics Pipeline
-----------------------------------------------------
Reads variant_manifest.json, generates a background image per unique
(model_id, color_variant) combo via Replicate (black-forest-labs/flux-2-pro),
saves PNGs under assets/generated/, and writes bg_image_path back into
each matching variant record in the manifest.

Usage:
    python generation_layer.py [--manifest path/to/manifest.json] [--dry-run]

Requirements:
    pip install replicate python-dotenv
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

PROMPT_SUFFIX = (
    " Background scene only — no musical instruments, no people, no text. "
    "Photorealistic. Cinematic lighting. Do not include any instrument in the frame."
)

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
    sanitized = sanitize_prompt(combo["creative_direction"], combo)
    return sanitized + PROMPT_SUFFIX


def unique_combos(variants: list[dict]) -> list[dict]:
    """Dedupe variants by (model_id, color_variant), preserving first occurrence."""
    seen: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
    for v in variants:
        key = (v["model_id"], v["color_variant"])
        if key not in seen:
            seen[key] = v
    return list(seen.values())


def download_output(output, dest: Path) -> None:
    """
    Replicate's flux-2-pro returns either a FileOutput object (newer SDK) or a URL
    string (older SDK). Handle both. FileOutput is iterable/readable; URL is a string.
    """
    if isinstance(output, list):
        output = output[0]

    if hasattr(output, "read"):
        dest.write_bytes(output.read())
    else:
        urllib.request.urlretrieve(str(output), dest)


def generate_background(prompt: str, dest: Path) -> bool:
    try:
        output = replicate.run(MODEL, input={"prompt": prompt})
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
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"✗ Manifest not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    combos = unique_combos(manifest["variants"])
    total_variants = len(manifest["variants"])

    if not args.dry_run:
        if not os.environ.get("REPLICATE_API_TOKEN"):
            print("✗ REPLICATE_API_TOKEN not set. Add it to .env or export it.")
            sys.exit(1)
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n→ Generating backgrounds for {len(combos)} unique combos "
          f"(applied to {total_variants} variant records)...")
    print("  [DRY RUN — no Replicate calls]\n" if args.dry_run else "")

    combo_to_bg: dict[tuple[str, str], str] = {}

    for i, combo in enumerate(combos, start=1):
        key = (combo["model_id"], combo["color_variant"])
        bg_filename = f"{combo['model_id']}_{combo['color_variant']}_bg.png"
        bg_path = GENERATED_DIR / bg_filename
        prompt = build_prompt(combo)

        print(f"  [{i}/{len(combos)}] {combo['model_id']} / {combo['color_variant']}")

        if args.dry_run:
            print(f"    → would save: {bg_path}")
            print(f"    prompt: {prompt}")
            combo_to_bg[key] = str(bg_path)
            continue

        if generate_background(prompt, bg_path):
            print(f"    ✓ saved: {bg_path}")
            combo_to_bg[key] = str(bg_path)

        if i < len(combos):
            time.sleep(SLEEP_BETWEEN_CALLS)

    if args.dry_run:
        print(f"\n  [DRY RUN] Would update {total_variants} variant records with bg_image_path.")
        return

    # Write bg_image_path back to every variant record whose combo was generated
    updated = 0
    for v in manifest["variants"]:
        key = (v["model_id"], v["color_variant"])
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
