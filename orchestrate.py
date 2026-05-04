"""
orchestrate.py — Yosuki Motion Graphics Pipeline
------------------------------------------------
Reads variant_manifest.json, emits a nexrender job JSON per variant at
jobs/{variant_id}.json, and shells out to nexrender-cli to render each
one via aerender. Prints a status table at the end.

Usage:
    python orchestrate.py [--manifest variant_manifest.json]
                          [--filter guitar-1]
                          [--jobs-only]

Requirements:
    npm install -g @nexrender/cli @nexrender/action-encode
    AERENDER_PATH must be set in .env (absolute path to the aerender binary).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

JOBS_ROOT = Path("jobs")


def file_uri(path: Path) -> str:
    """nexrender accepts file:// URIs for local assets."""
    return path.resolve().as_uri()


def build_job(variant: dict, template_aep: Path, comp: str) -> dict:
    output_abs = str(Path(variant["output_path"]).resolve())

    # useOriginal=true tells nexrender to reference local file:// assets in place
    # instead of copying them to /tmp/nexrender (which has a pathing bug on deep
    # absolute source paths — see @nexrender/core download.js:163-166).
    return {
        "template": {
            "src": file_uri(template_aep),
            "composition": comp,
            "useOriginal": True,
        },
        "assets": [
            {"type": "image", "layerName": "bg_image",      "src": file_uri(Path(variant["bg_image_path"])),      "useOriginal": True},
            {"type": "image", "layerName": "product_image", "src": file_uri(Path(variant["product_image_path"])), "useOriginal": True},
            {"type": "image", "layerName": "logo",          "src": file_uri(Path(variant["logo_path"])),          "useOriginal": True},
            {"type": "data",  "layerName": "tagline", "property": "Source Text", "value": variant["tagline"]},
            {"type": "data",  "layerName": "cta",     "property": "Source Text", "value": variant["cta"]},
        ],
        "actions": {
            "postrender": [
                {"module": "@nexrender/action-encode", "preset": "mp4", "output": output_abs},
            ],
        },
    }


def write_job(variant: dict, jobs_dir: Path, template_aep: Path, comp: str) -> Path:
    job = build_job(variant, template_aep, comp)
    job_path = jobs_dir / f"{variant['variant_id']}.json"
    with open(job_path, "w") as f:
        json.dump(job, f, indent=2)
    return job_path


def render_job(job_path: Path, aerender_path: str) -> tuple[bool, str]:
    cmd = ["nexrender-cli", "-f", str(job_path), "-b", aerender_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode == 0:
            return True, "rendered"
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
        return False, f"nexrender exit {result.returncode}: {tail[0][:120]}"
    except subprocess.TimeoutExpired:
        return False, "timeout after 900s"
    except FileNotFoundError:
        return False, "nexrender-cli not on PATH"


def print_summary(rows: list[tuple[str, str, str]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    header = ("variant_id", "status", "output_path")
    widths = [max(widths[i], len(header[i])) for i in range(3)]

    def fmt(row):
        return "  ".join(row[i].ljust(widths[i]) for i in range(3))

    print("\n" + fmt(header))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))


def main():
    parser = argparse.ArgumentParser(description="Yosuki pipeline — Orchestration Layer")
    parser.add_argument("--manifest", default="variant_manifest.json", help="Path to variant manifest")
    parser.add_argument("--filter", action="append", default=None,
                        help="Substring filter on variant_id. Repeatable — all must match "
                             "(e.g. --filter guitar --filter 1920x1080).")
    parser.add_argument("--jobs-only", action="store_true",
                        help="Write job JSONs but do not invoke nexrender")
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
    template_aep = Path(manifest["template"]["aep_path"])
    comps_by_ratio: dict[str, str] = manifest["template"]["comps_by_ratio"]
    jobs_dir = JOBS_ROOT / slug

    if not template_aep.exists():
        print(f"✗ Template AEP not found: {template_aep}")
        sys.exit(1)

    variants = manifest["variants"]
    if args.filter:
        variants = [v for v in variants if all(f in v["variant_id"] for f in args.filter)]
        if not variants:
            print(f"✗ No variants match filters {args.filter}")
            sys.exit(1)

    aerender_path = os.environ.get("AERENDER_PATH")
    if not args.jobs_only:
        if not aerender_path:
            print("✗ AERENDER_PATH not set. Add it to .env or export it.")
            sys.exit(1)
        if not Path(aerender_path).exists():
            print(f"✗ AERENDER_PATH points to missing binary: {aerender_path}")
            sys.exit(1)

    jobs_dir.mkdir(parents=True, exist_ok=True)
    # Ensure every target output directory exists
    for v in variants:
        Path(v["output_path"]).parent.mkdir(parents=True, exist_ok=True)

    print(f"\n→ Orchestrating {len(variants)} variants "
          f"({'jobs only' if args.jobs_only else 'jobs + render'})")
    if args.filter:
        print(f"  Filters (AND): {args.filter}")

    rows: list[tuple[str, str, str]] = []

    for i, variant in enumerate(variants, start=1):
        vid = variant["variant_id"]
        ratio = variant["aspect_ratio"]

        comp = comps_by_ratio.get(ratio)
        if comp is None:
            print(f"  [{i}/{len(variants)}] {vid} — ✗ no comp mapped for aspect_ratio '{ratio}' in template.comps_by_ratio")
            rows.append((vid, "skipped: unknown ratio", variant["output_path"]))
            continue

        if not variant.get("bg_image_path"):
            print(f"  [{i}/{len(variants)}] {vid} — ⊘ no bg_image_path (run generation layer first)")
            rows.append((vid, "skipped: no background", variant["output_path"]))
            continue

        job_path = write_job(variant, jobs_dir, template_aep, comp)
        print(f"  [{i}/{len(variants)}] {vid} → {job_path}")

        if args.jobs_only:
            rows.append((vid, "job written", str(job_path)))
            continue

        ok, msg = render_job(job_path, aerender_path)
        status_icon = "✓" if ok else "✗"
        print(f"    {status_icon} {msg}")
        rows.append((vid, msg, variant["output_path"]))

    print_summary(rows)

    if args.jobs_only:
        written = sum(1 for r in rows if r[1] == "job written")
        skipped = len(rows) - written
        msg = f"\n✓ {written} job JSONs written to {jobs_dir}/"
        if skipped:
            msg += f" ({skipped} variant(s) skipped)"
        print(msg)
    else:
        ok_count = sum(1 for r in rows if r[1] == "rendered")
        print(f"\n✓ Rendered {ok_count}/{len(rows)} variants")


if __name__ == "__main__":
    main()
