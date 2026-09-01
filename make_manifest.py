"""Build a gt-annotator manifest from a query source file.

Supported source shapes:
  A) mapping: ``{"<item_id>": {"<image_field>": path, "<query_field>": text}}``
     (the main project's official Test template uses this shape with fields
     visible/infrared/depth/query; pass ``--image-field visible``)
  B) list: ``[{"id": "...", "<image_field>": path, "<query_field>": text}]``

Usage::

    python make_manifest.py --source /path/queries.json \
        --images-root /path/Test --image-field visible \
        --name rgbdt-test --out data/manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_items(source: object, image_field: str, query_field: str) -> list[dict]:
    if isinstance(source, dict):
        entries = []
        for item_id, entry in source.items():
            if not isinstance(item_id, str) or not item_id:
                raise ValueError(f"source mapping key is not a string id: {item_id!r}")
            if not isinstance(entry, dict):
                raise ValueError(f"source entry {item_id!r} is not a JSON object")
            merged = dict(entry)
            merged["id"] = item_id
            entries.append(merged)
    elif isinstance(source, list):
        entries = source
    else:
        raise ValueError("source must be a JSON object (mapping) or a JSON array (list)")

    items: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"source entry is not a JSON object: {entry!r}")
        item_id = entry.get("id")
        image = entry.get(image_field)
        query = entry.get(query_field)
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError(f"source entry missing string 'id': {entry!r}")
        if item_id in seen:
            raise ValueError(f"duplicate item id: {item_id!r}")
        seen.add(item_id)
        if not isinstance(image, str) or not image:
            raise ValueError(
                f"item {item_id!r} missing non-empty {image_field!r} image path"
            )
        if "\\" in image:
            raise ValueError(f"item {item_id!r} image path must use POSIX separators")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"item {item_id!r} missing non-empty {query_field!r} query")
        items.append({"id": item_id, "image": image, "query": query})
    return items


def check_images(items: list[dict], images_root: Path) -> list[str]:
    missing = []
    for item in items:
        candidate = Path(item["image"])
        path = candidate if candidate.is_absolute() else images_root / candidate
        if not path.is_file():
            missing.append(item["image"])
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build a gt-annotator manifest")
    parser.add_argument("--source", required=True, help="query source JSON file")
    parser.add_argument("--images-root", required=True, help="root directory for image paths")
    parser.add_argument("--image-field", default="visible")
    parser.add_argument("--query-field", default="query")
    parser.add_argument("--name", default=None, help="manifest name (default: source stem)")
    parser.add_argument("--out", required=True, help="output manifest path")
    parser.add_argument(
        "--skip-image-check",
        action="store_true",
        help="do not verify that every image file exists",
    )
    args = parser.parse_args(argv)

    source_path = Path(args.source).expanduser()
    images_root = Path(args.images_root).expanduser().resolve()
    if not source_path.is_file():
        print(f"source not found: {source_path}", file=sys.stderr)
        return 2
    if not images_root.is_dir():
        print(f"images_root not found: {images_root}", file=sys.stderr)
        return 2

    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
        items = build_items(source, args.image_field, args.query_field)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"invalid source: {exc}", file=sys.stderr)
        return 2

    if not args.skip_image_check:
        missing = check_images(items, images_root)
        if missing:
            preview = ", ".join(missing[:5])
            print(
                f"{len(missing)} image files missing under {images_root} "
                f"(first: {preview}); fix the source or pass --skip-image-check",
                file=sys.stderr,
            )
            return 2

    manifest = {
        "name": args.name or source_path.stem,
        "images_root": str(images_root),
        "items": items,
    }
    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(out_path)
    print(f"items={len(items)} unique_images={len({i['image'] for i in items})}")
    print(f"images_root={images_root}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
