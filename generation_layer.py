"""
generation_layer.py — Yosuki Motion Graphics Pipeline
-----------------------------------------------------
Reads variant_manifest.json, deduplicates by (model_id, color_variant,
aspect_ratio), serializes each variant's flux_prompt JSON object and sends it
to Replicate (flux-2-pro) at the correct pixel dimensions, saves PNGs under
assets/generated/, and writes bg_image_path back into matching variant records.
String fields in the flux_prompt are sanitized against product/anatomy words
as a safety net in case Claude slips a disallowed word in.

Usage:
    python generation_layer.py [--manifest variant_manifest.json]
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
GENERATED_ROOT = Path("assets/generated")
SLEEP_BETWEEN_CALLS = 2  # seconds

PROMPT_SUFFIX = " Background scene only. Empty of people and objects. Photorealistic. Cinematic."


def flux_dims_for_ratio(ratio: str) -> dict:
    """Parse a 'WxH' aspect ratio string and return Replicate flux-2-pro params,
    rounding W/H up to multiples of 16 (Flux requirement)."""
    w_str, h_str = ratio.split("x")
    w, h = int(w_str), int(h_str)
    ceil16 = lambda n: ((n + 15) // 16) * 16
    return {"aspect_ratio": "custom", "width": ceil16(w), "height": ceil16(h)}

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
    flux_prompt = combo.get("flux_prompt")
    if not isinstance(flux_prompt, dict):
        raise ValueError(
            f"Variant {combo.get('variant_id')} missing flux_prompt object. "
            f"Did you run input_layer.py against the brief first?"
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
    parser = argparse.ArgumentParser(description="Yosuki pipeline — Generation Layer")
    parser.add_argument("--manifest", default="variant_manifest.json", help="Path to variant manifest")
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

    if manifest.get("schema_version") != 3:
        print(f"⚠  Expected schema_version=3, got {manifest.get('schema_version')}. "
              f"Re-run input_layer.py to regenerate the manifest.")
        sys.exit(1)

    slug = manifest["slug"]
    generated_dir = GENERATED_ROOT / slug

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
        generated_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n→ Generating backgrounds for {len(combos)} unique combos "
          f"(applied to {total_variants} variant records)...")
    print("  [DRY RUN — no Replicate calls]\n" if args.dry_run else "")

    combo_to_bg: dict[tuple[str, str, str], str] = {}

    for i, combo in enumerate(combos, start=1):
        aspect_ratio = combo["aspect_ratio"]
        key = (combo["model_id"], combo["color_variant"], aspect_ratio)
        bg_filename = f"{combo['model_id']}_{combo['color_variant']}_{aspect_ratio}_bg.png"
        bg_path = generated_dir / bg_filename

        try:
            prompt = build_prompt(combo)
        except ValueError as e:
            print(f"  [{i}/{len(combos)}] ✗ {e}")
            continue

        try:
            ratio_params = flux_dims_for_ratio(aspect_ratio)
        except (ValueError, IndexError):
            print(f"  [{i}/{len(combos)}] ✗ Cannot parse aspect_ratio '{aspect_ratio}' as 'WxH' — skipping")
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
