"""
input_layer.py — Yosuki Motion Graphics Pipeline
-------------------------------------------------
Reads campaign_brief.json, calls Claude API to generate structured
copy (tagline, CTA, creative_direction) per product variant,
and outputs variant_manifest.json consumed by generation_layer.py
and orchestrate.py.

Usage:
    python input_layer.py [--brief path/to/brief.json] [--out path/to/manifest.json]

Requirements:
    pip install anthropic jsonschema python-dotenv
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from jsonschema import ValidationError, validate

load_dotenv()

# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

BRIEF_SCHEMA = {
    "type": "object",
    "required": ["brand", "campaign", "products"],
    "properties": {
        "brand": {
            "type": "object",
            "required": ["name", "logo_path", "primary_color", "tone", "target_audience"],
            "properties": {
                "name": {"type": "string"},
                "logo_path": {"type": "string"},
                "primary_color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
                "tone": {"type": "string"},
                "target_audience": {"type": "string"},
            },
        },
        "campaign": {
            "type": "object",
            "required": ["name", "duration_seconds", "aspect_ratios", "output_dir"],
            "properties": {
                "name": {"type": "string"},
                "duration_seconds": {"type": "number", "minimum": 5, "maximum": 10},
                "aspect_ratios": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "output_dir": {"type": "string"},
            },
        },
        "products": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["product_line", "model_id", "model_name", "variants", "image_paths"],
                "properties": {
                    "product_line": {"type": "string", "enum": ["guitar", "piano", "saxophone"]},
                    "model_id": {"type": "string"},
                    "model_name": {"type": "string"},
                    "variants": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "image_paths": {"type": "object"},
                    "render_path": {"type": "string"},
                    "tagline_hint": {"type": "string"},
                    "scene_direction": {"type": "string"},
                },
            },
        },
    },
}

COPY_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["tagline", "cta", "creative_direction"],
    "properties": {
        "tagline": {"type": "string", "maxLength": 60},
        "cta": {"type": "string", "maxLength": 40},
        "creative_direction": {"type": "string", "maxLength": 300},
    },
}


# ---------------------------------------------------------------------------
# Claude API — structured copy generation
# ---------------------------------------------------------------------------

def build_copy_prompt(brand: dict, product: dict, variant: str) -> str:
    """
    Builds the user prompt for Claude. The model will return JSON only.
    Tagline_hint and scene_direction from the brief seed the output.
    """
    tagline_hint = product.get("tagline_hint", "")
    scene_direction = product.get("scene_direction", "")

    return f"""You are a copywriter for {brand["name"]}, a premium Japanese musical instrument brand.

Brand tone: {brand["tone"]}
Target audience: {brand["target_audience"]}

Product: {product["model_name"]} ({product["product_line"]}, {variant} finish)
Tagline seed: "{tagline_hint}"
Scene direction: "{scene_direction}"

Generate ad copy for a {product["product_line"]} ad. Return ONLY valid JSON with this exact structure:

{{
  "tagline": "<punchy tagline under 60 characters, inspired by the seed>",
  "cta": "<call-to-action under 40 characters>",
  "creative_direction": "<one paragraph directing the visual tone, lighting, mood, and composition of the background scene for this specific variant. Reference the scene_direction seed and the product color/finish.>"
}}

Rules:
- tagline must be under 60 characters
- cta must be under 40 characters  
- creative_direction must be under 300 characters
- Return ONLY the JSON object. No preamble, no markdown, no code fences."""


def generate_copy(client: anthropic.Anthropic, brand: dict, product: dict, variant: str) -> dict:
    """
    Calls Claude and returns validated structured copy for one variant.
    Retries once on validation failure with a stricter prompt.
    """
    for attempt in range(2):
        prompt = build_copy_prompt(brand, product, variant)

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip accidental markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            copy = json.loads(raw)
            validate(instance=copy, schema=COPY_RESPONSE_SCHEMA)

            # Enforce length constraints (schema maxLength is advisory for Claude)
            if len(copy["tagline"]) > 60:
                copy["tagline"] = copy["tagline"][:57] + "..."
            if len(copy["cta"]) > 40:
                copy["cta"] = copy["cta"][:37] + "..."
            if len(copy["creative_direction"]) > 300:
                copy["creative_direction"] = copy["creative_direction"][:297] + "..."

            return copy

        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == 0:
                print(f"    ⚠  Copy parse failed (attempt 1), retrying... [{e}]")
                continue
            else:
                print(f"    ✗  Copy generation failed after 2 attempts for {product['model_id']}/{variant}")
                print(f"    Raw response: {raw[:200]}")
                # Return safe fallback so pipeline continues
                return {
                    "tagline": f"The {product['model_name']}.",
                    "cta": "Discover More",
                    "creative_direction": product.get("scene_direction", "Clean studio backdrop."),
                }


# ---------------------------------------------------------------------------
# Variant expansion
# ---------------------------------------------------------------------------

def expand_variants(brief: dict, client: anthropic.Anthropic) -> list[dict]:
    """
    Expands products × variants × aspect_ratios into a flat list of variant records.
    Calls Claude once per product-variant combo (shared across aspect ratios to save tokens).
    """
    brand = brief["brand"]
    campaign = brief["campaign"]
    variants_out = []

    total = sum(len(p["variants"]) for p in brief["products"])
    print(f"\n→ Generating copy for {total} product-variant combos across {len(campaign['aspect_ratios'])} aspect ratios...\n")

    for product in brief["products"]:
        for color_variant in product["variants"]:

            print(f"  Generating copy: {product['model_id']} / {color_variant}")
            copy = generate_copy(client, brand, product, color_variant)
            print(f"    ✓ tagline: \"{copy['tagline']}\"")
            print(f"    ✓ cta:     \"{copy['cta']}\"")

            # Resolve product image path for this variant
            image_path = product["image_paths"].get(color_variant)
            if not image_path:
                print(f"    ⚠  No image_path found for variant '{color_variant}' in {product['model_id']}")

            # One record per aspect ratio
            for aspect_ratio in campaign["aspect_ratios"]:
                safe_ratio = aspect_ratio.replace("x", "x")  # normalize separator
                variant_id = f"{product['model_id']}_{color_variant}_{safe_ratio}"
                output_filename = f"yosuki_{variant_id}.mp4"
                output_path = str(Path(campaign["output_dir"]) / output_filename)

                variants_out.append({
                    "variant_id": variant_id,
                    "product_line": product["product_line"],
                    "model_id": product["model_id"],
                    "model_name": product["model_name"],
                    "color_variant": color_variant,
                    "aspect_ratio": aspect_ratio,
                    "product_image_path": image_path,
                    "render_path": product.get("render_path"),         # optional .glb
                    "bg_image_path": None,                             # populated by generation_layer.py
                    "tagline": copy["tagline"],
                    "cta": copy["cta"],
                    "creative_direction": copy["creative_direction"],
                    "scene_direction": product.get("scene_direction"), # raw seed, kept for reference
                    "logo_path": brand["logo_path"],
                    "primary_color": brand["primary_color"],
                    "duration_seconds": campaign["duration_seconds"],
                    "output_path": output_path,
                })

    return variants_out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Yosuki pipeline — Input Layer")
    parser.add_argument("--brief", default="campaign_brief.json", help="Path to campaign brief JSON")
    parser.add_argument("--out", default="variant_manifest.json", help="Output manifest path")
    args = parser.parse_args()

    # Load brief
    brief_path = Path(args.brief)
    if not brief_path.exists():
        print(f"✗ Brief not found: {brief_path}")
        sys.exit(1)

    with open(brief_path) as f:
        brief = json.load(f)

    # Validate brief schema
    try:
        validate(instance=brief, schema=BRIEF_SCHEMA)
        print(f"✓ Brief validated: {brief_path}")
    except ValidationError as e:
        print(f"✗ Brief schema error: {e.message}")
        sys.exit(1)

    # Init Anthropic client
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("✗ ANTHROPIC_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Expand and generate
    variants = expand_variants(brief, client)

    # Build manifest
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "campaign": brief["campaign"]["name"],
        "brand": brief["brand"]["name"],
        "total_variants": len(variants),
        "variants": variants,
    }

    # Write manifest
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n✓ Manifest written: {out_path}")
    print(f"  Total variants: {len(variants)}")
    print(f"  Next step: python generation_layer.py --manifest {out_path}")


if __name__ == "__main__":
    main()