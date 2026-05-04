"""
input_layer.py — Yosuki Motion Graphics Pipeline
------------------------------------------------
Reads campaign_brief.json, calls Claude per (product, color_variant) pair to
generate ad copy + a structured flux_prompt object, validates against
COPY_RESPONSE_SCHEMA, and writes variant_manifest.json (one record per
product × variant × aspect_ratio).

Usage:
    python input_layer.py [--brief campaign_brief.json] [--out variant_manifest.json]
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
# Truncation caps (enforced in Python; schema caps are advisory to Claude)
# ---------------------------------------------------------------------------

TAGLINE_CAP = 40
CTA_CAP = 25
CREATIVE_DIRECTION_CAP = 300
FLUX_STRING_CAP = 150  # applies to: scene, style, lighting, mood, background, composition

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

FLUX_PROMPT_SCHEMA = {
    "type": "object",
    "required": ["scene", "style", "color_palette", "lighting", "mood", "background", "composition", "camera"],
    "properties": {
        "scene":        {"type": "string"},
        "style":        {"type": "string"},
        "color_palette":{"type": "array", "items": {"type": "string"}, "minItems": 1},
        "lighting":     {"type": "string"},
        "mood":         {"type": "string"},
        "background":   {"type": "string"},
        "composition":  {"type": "string"},
        "camera": {
            "type": "object",
            "required": ["angle", "lens", "depth_of_field"],
            "properties": {
                "angle":          {"type": "string"},
                "lens":           {"type": "string"},
                "depth_of_field": {"type": "string"},
            },
        },
    },
}

COPY_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["tagline", "cta", "creative_direction", "flux_prompt"],
    "properties": {
        "tagline": {"type": "string"},
        "cta": {"type": "string"},
        "creative_direction": {"type": "string"},
        "flux_prompt": FLUX_PROMPT_SCHEMA,
    },
}

# Flux-string fields that get Python-truncated to FLUX_STRING_CAP
FLUX_STRING_FIELDS = ("scene", "style", "lighting", "mood", "background", "composition")


# ---------------------------------------------------------------------------
# Claude API — structured copy generation
# ---------------------------------------------------------------------------

def build_copy_prompt(brand: dict, product: dict, variant: str) -> str:
    tagline_hint = product.get("tagline_hint", "")
    scene_direction = product.get("scene_direction", "")

    return f"""You are a copywriter and art director for {brand["name"]}, a premium Japanese musical instrument brand.

Brand tone: {brand["tone"]}
Target audience: {brand["target_audience"]}
Brand crimson: {brand["primary_color"]}

Product: {product["model_name"]} ({product["product_line"]}, {variant} finish)
Tagline seed: "{tagline_hint}"
Scene direction seed: "{scene_direction}"

Generate ad copy AND a Flux image-generation prompt. Return ONLY valid JSON with this exact structure:

{{
  "tagline": "<punchy tagline under {TAGLINE_CAP} characters, inspired by the seed>",
  "cta": "<call-to-action under {CTA_CAP} characters>",
  "creative_direction": "<one paragraph under {CREATIVE_DIRECTION_CAP} characters directing the visual tone, lighting, mood, and composition of the background scene for this specific variant. Reference the scene direction seed and the product color/finish.>",
  "flux_prompt": {{
    "scene":        "<abstract atmospheric scene — light fields, color gradients, mood>",
    "style":        "<specific real camera + lens, e.g. 'Hasselblad X2D 100C, 80mm f/4'>",
    "color_palette": ["<hex>", "<hex>", "..."],
    "lighting":     "<description of light field, gradient, or luminous quality>",
    "mood":         "<mood keywords>",
    "background":   "<abstract backdrop suited to a premium instrument composited over it>",
    "composition":  "<composition notes — e.g. horizontal gradient bands, radial, vertical beam>",
    "camera": {{
      "angle":          "<camera angle>",
      "lens":           "<lens spec>",
      "depth_of_field": "<depth-of-field description>"
    }}
  }}
}}

Rules:
- tagline must be under {TAGLINE_CAP} characters
- cta must be under {CTA_CAP} characters
- Return ONLY the JSON object. No preamble, no markdown, no code fences.

Critical creative rules for flux_prompt:
- Abstract and atmospheric ONLY — no rooms, floors, furniture, architecture, or recognisable spaces
- Describe light fields, color gradients, and mood — not things
- No objects, no people, no instruments of any kind
- "style" must reference a specific real camera + lens (e.g. Hasselblad X2D 100C, 80mm f/4)
- "color_palette" must be a JSON array of hex strings and must include brand crimson {brand["primary_color"]}
- "background" must suit a premium instrument composited over it
- Think: fine art color field photography, not editorial photography
- Each string field (scene, style, lighting, mood, background, composition) must be under {FLUX_STRING_CAP} characters"""


def truncate(s: str, cap: int) -> str:
    return s if len(s) <= cap else s[: cap - 3] + "..."


def apply_truncation(copy: dict) -> dict:
    copy["tagline"] = truncate(copy["tagline"], TAGLINE_CAP)
    copy["cta"] = truncate(copy["cta"], CTA_CAP)
    copy["creative_direction"] = truncate(copy["creative_direction"], CREATIVE_DIRECTION_CAP)
    fp = copy["flux_prompt"]
    for field in FLUX_STRING_FIELDS:
        fp[field] = truncate(fp[field], FLUX_STRING_CAP)
    return copy


def safe_fallback(product: dict, brand: dict) -> dict:
    """Minimal valid copy so pipeline continues if Claude fails twice."""
    return {
        "tagline": f"The {product['model_name']}."[:TAGLINE_CAP],
        "cta": "Discover"[:CTA_CAP],
        "creative_direction": product.get("scene_direction", "Premium studio backdrop."),
        "flux_prompt": {
            "scene": "Abstract dark void with warm gradient light field",
            "style": "fine art color field photography, Hasselblad X2D 100C, 80mm f/4",
            "color_palette": [brand["primary_color"], "#1A1A1A", "#8B6914"],
            "lighting": "warm gradient fading into deep black, soft and diffused",
            "mood": "quiet, premium, contemplative",
            "background": "seamless gradient field suited to a premium instrument composite",
            "composition": "horizontal gradient bands, empty, minimal",
            "camera": {"angle": "flat perpendicular", "lens": "80mm", "depth_of_field": "infinite"},
        },
    }


def generate_copy(client: anthropic.Anthropic, brand: dict, product: dict, variant: str) -> dict:
    for attempt in range(2):
        prompt = build_copy_prompt(brand, product, variant)

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            copy = json.loads(raw)
            validate(instance=copy, schema=COPY_RESPONSE_SCHEMA)
            return apply_truncation(copy)

        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == 0:
                print(f"    ⚠  Copy parse failed (attempt 1), retrying... [{e}]")
                continue
            print(f"    ✗  Copy generation failed after 2 attempts for {product['model_id']}/{variant}")
            print(f"    Raw response: {raw[:200]}")
            return safe_fallback(product, brand)


# ---------------------------------------------------------------------------
# Variant expansion
# ---------------------------------------------------------------------------

def expand_variants(brief: dict, client: anthropic.Anthropic) -> list[dict]:
    brand = brief["brand"]
    campaign = brief["campaign"]
    variants_out = []

    total = sum(len(p["variants"]) for p in brief["products"])
    print(f"\n→ Generating copy + flux_prompt for {total} product-variant combos "
          f"across {len(campaign['aspect_ratios'])} aspect ratios...\n")

    for product in brief["products"]:
        for color_variant in product["variants"]:

            print(f"  Generating: {product['model_id']} / {color_variant}")
            copy = generate_copy(client, brand, product, color_variant)
            print(f"    ✓ tagline: \"{copy['tagline']}\"")
            print(f"    ✓ cta:     \"{copy['cta']}\"")
            print(f"    ✓ flux_prompt.scene: \"{copy['flux_prompt']['scene']}\"")

            image_path = product["image_paths"].get(color_variant)
            if not image_path:
                print(f"    ⚠  No image_path found for variant '{color_variant}' in {product['model_id']}")

            for aspect_ratio in campaign["aspect_ratios"]:
                variant_id = f"{product['model_id']}_{color_variant}_{aspect_ratio}"
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
                    "render_path": product.get("render_path"),
                    "bg_image_path": None,
                    "tagline": copy["tagline"],
                    "cta": copy["cta"],
                    "creative_direction": copy["creative_direction"],
                    "flux_prompt": copy["flux_prompt"],
                    "scene_direction_seed": product.get("scene_direction"),
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

    brief_path = Path(args.brief)
    if not brief_path.exists():
        print(f"✗ Brief not found: {brief_path}")
        sys.exit(1)

    with open(brief_path) as f:
        brief = json.load(f)

    try:
        validate(instance=brief, schema=BRIEF_SCHEMA)
        print(f"✓ Brief validated: {brief_path}")
    except ValidationError as e:
        print(f"✗ Brief schema error: {e.message}")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("✗ ANTHROPIC_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    variants = expand_variants(brief, client)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 2,
        "campaign": brief["campaign"]["name"],
        "brand": brief["brand"]["name"],
        "total_variants": len(variants),
        "variants": variants,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n✓ Manifest written: {out_path}")
    print(f"  Total variants: {len(variants)}")
    print(f"  Next step: python generation_layer.py --manifest {out_path}")


if __name__ == "__main__":
    main()
