"""gt-annotator backend: stdlib-only HTTP server for human box annotation.

Serves a manifest of (image, query) items and persists human-drawn boxes as
``{item_id: [x1, y1, x2, y2]}`` (normalized 0-1 XYXY), matching prediction
files in the main project so annotations can be scored directly.

Usage::

    python server.py --manifest data/manifest.json --data-dir data \
        --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from bbox import normalize_bbox
from store import AnnotationStore

PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
MAX_BODY_BYTES = 1_000_000
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def load_manifest(path: Path) -> tuple[str, Path, list[dict]]:
    """Return ``(name, images_root, items)`` from a manifest JSON file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"manifest must be a JSON object: {path}")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError(f"manifest must contain a non-empty 'items' list: {path}")
    images_root = Path(str(data.get("images_root", ""))).expanduser()
    if not images_root.is_absolute():
        images_root = (Path(path).resolve().parent / images_root).resolve()
    if not str(images_root):
        raise ValueError("manifest must set 'images_root'")
    name = str(data.get("name") or Path(path).stem)
    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ValueError("manifest items must be JSON objects")
        item_id = entry.get("id")
        image = entry.get("image")
        query = entry.get("query")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"manifest item missing string 'id': {entry!r}")
        if item_id in seen:
            raise ValueError(f"manifest duplicate item id: {item_id!r}")
        seen.add(item_id)
        if not isinstance(image, str) or not image:
            raise ValueError(f"manifest item {item_id!r} missing string 'image'")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"manifest item {item_id!r} missing non-empty 'query'")
        items.append({"id": item_id, "image": image, "query": query})
    return name, images_root, items


def load_translations(path: Path) -> dict[str, str]:
    """Load the optional ``{item_id: chinese}`` sidecar produced by translate.py."""
    if not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"translations must be a JSON object: {path}")
    return {str(key): str(value) for key, value in data.items()}


class AnnotatorState:
    """Immutable per-run context shared across request handler threads."""

    def __init__(
        self,
        *,
        manifest_name: str,
        images_root: Path,
        items: list[dict],
        translations: dict[str, str],
        store: AnnotationStore,
    ) -> None:
        self.manifest_name = manifest_name
        self.images_root = Path(images_root).resolve()
        self.items = items
        self.item_by_id = {item["id"]: item for item in items}
        self.image_paths = {item["image"] for item in items}
        self.translations = translations
        self.store = store

    def session_payload(self) -> dict:
        payload_items = []
        for item in self.items:
            meta = self.store.meta(item["id"]) or {}
            payload_items.append(
                {
                    "id": item["id"],
                    "image_url": "/image?src=" + quote(item["image"]),
                    "query_en": item["query"],
                    "query_zh": self.translations.get(item["id"]),
                    "bbox": self.store.get(item["id"]),
                    "annotator": meta.get("annotator"),
                }
            )
        annotated = sum(1 for entry in payload_items if entry["bbox"] is not None)
        return {
            "manifest": self.manifest_name,
            "total_items": len(self.items),
            "annotated": annotated,
            "items": payload_items,
        }

    def progress_payload(self) -> dict:
        images = {item["image"] for item in self.items}
        annotated_images = {
            item["image"]
            for item in self.items
            if self.store.get(item["id"]) is not None
        }
        manifest_ids = {item["id"] for item in self.items}
        absent_in_manifest = set(self.store.absent_items()) & manifest_ids
        return {
            "manifest": self.manifest_name,
            "total_items": len(self.items),
            "annotated": self.store.annotated_count(),
            "absent": len(absent_in_manifest),
            "total_images": len(images),
            "annotated_images": len(annotated_images),
        }

    def resolve_image(self, src: str) -> Path | None:
        # Membership check against manifest entries makes traversal impossible:
        # only exact manifest-listed path strings are ever served.
        if src not in self.image_paths:
            return None
        candidate = Path(src)
        path = candidate if candidate.is_absolute() else self.images_root / candidate
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_file():
            return None
        return resolved


class AnnotationHandler(BaseHTTPRequestHandler):
    server_version = "gt-annotator/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> AnnotatorState:
        return self.server.annotator_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(
            f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}\n"
        )

    # -- response helpers --------------------------------------------------

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, cache: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("invalid body size")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/session":
            return self._send_json(self.state.session_payload())
        if parsed.path == "/api/progress":
            return self._send_json(self.state.progress_payload())
        if parsed.path == "/image":
            src = (parse_qs(parsed.query).get("src") or [""])[0]
            resolved = self.state.resolve_image(src)
            if resolved is None:
                return self._send_json({"error": "image not found"}, 404)
            content_type = (
                STATIC_TYPES.get(resolved.suffix.lower())
                or mimetypes.guess_type(str(resolved))[0]
                or "application/octet-stream"
            )
            body = resolved.read_bytes()
            return self._send_bytes(body, content_type, cache="no-cache")
        return self._serve_static(parsed.path)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        item_id = self._match_item_route(path, "/bbox")
        if item_id is not None:
            return self._handle_put_bbox(item_id)
        item_id = self._match_item_route(path, "/absent")
        if item_id is not None:
            return self._handle_put_absent(item_id)
        return self._send_json({"error": "not found"}, 404)

    def _handle_put_bbox(self, item_id: str) -> None:
        if item_id not in self.state.item_by_id:
            return self._send_json({"error": f"unknown item id: {item_id}"}, 404)
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json({"error": str(exc)}, 400)
        try:
            bbox = normalize_bbox(body.get("bbox"))
        except ValueError as exc:
            return self._send_json({"error": f"invalid bbox: {exc}"}, 400)
        annotator = body.get("annotator")
        if annotator is not None and (not isinstance(annotator, str) or len(annotator) > 64):
            return self._send_json({"error": "annotator must be a string of at most 64 chars"}, 400)
        saved = self.state.store.set(item_id, bbox, annotator)
        return self._send_json({"id": item_id, "bbox": saved, "annotated": True})

    def _handle_put_absent(self, item_id: str) -> None:
        if item_id not in self.state.item_by_id:
            return self._send_json({"error": f"unknown item id: {item_id}"}, 404)
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json({"error": str(exc)}, 400)
        annotator = body.get("annotator")
        if not isinstance(annotator, str) or not annotator.strip() or len(annotator) > 64:
            return self._send_json(
                {"error": "annotator (non-empty string, max 64 chars) is required to confirm absence"},
                400,
            )
        record = self.state.store.set_absent(item_id, annotator.strip())
        return self._send_json(
            {"id": item_id, "bbox": None, "annotator": record["annotator"], "annotated": False}
        )

    def do_DELETE(self) -> None:
        item_id = self._match_item_route(urlparse(self.path).path, "/bbox")
        if item_id is None:
            return self._send_json({"error": "not found"}, 404)
        if item_id not in self.state.item_by_id:
            return self._send_json({"error": f"unknown item id: {item_id}"}, 404)
        self.state.store.delete(item_id)
        return self._send_json({"id": item_id, "bbox": None, "annotated": False})

    @staticmethod
    def _match_item_route(path: str, suffix: str) -> str | None:
        prefix = "/api/item/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        middle = path[len(prefix):-len(suffix)]
        if not middle or "/" in middle:
            return None
        return unquote(middle)

    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        web_root = WEB_ROOT.resolve()
        try:
            candidate = (web_root / rel).resolve()
            candidate.relative_to(web_root)
        except (OSError, ValueError):
            return self._send_json({"error": "not found"}, 404)
        if not candidate.is_file():
            return self._send_json(
                {"error": "frontend not built yet; web/index.html missing"}, 404
            )
        content_type = (
            STATIC_TYPES.get(candidate.suffix.lower())
            or mimetypes.guess_type(str(candidate))[0]
            or "application/octet-stream"
        )
        return self._send_bytes(candidate.read_bytes(), content_type, cache="no-cache")


def create_server(
    *,
    manifest_path: str | Path,
    data_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 0,
    translations_path: str | Path | None = None,
) -> tuple[ThreadingHTTPServer, AnnotatorState]:
    manifest_path = Path(manifest_path).expanduser().resolve()
    name, images_root, items = load_manifest(manifest_path)
    if not images_root.exists():
        raise FileNotFoundError(f"manifest images_root does not exist: {images_root}")
    if translations_path is None:
        translations_path = Path(data_dir) / "translations.json"
    translations = load_translations(Path(translations_path))
    store = AnnotationStore(data_dir)
    state = AnnotatorState(
        manifest_name=name,
        images_root=images_root,
        items=items,
        translations=translations,
        store=store,
    )
    server = ThreadingHTTPServer((host, port), AnnotationHandler)
    server.daemon_threads = True
    server.annotator_state = state  # type: ignore[attr-defined]
    return server, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gt-annotator backend server")
    parser.add_argument("--manifest", required=True, help="path to manifest.json")
    parser.add_argument("--data-dir", default="data", help="annotation output directory")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--translations", default=None, help="translations sidecar override")
    args = parser.parse_args(argv)

    try:
        server, state = create_server(
            manifest_path=args.manifest,
            data_dir=args.data_dir,
            host=args.host,
            port=args.port,
            translations_path=args.translations,
        )
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 2

    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else str(host)
    empty_queries = [item["id"] for item in state.items if not item["query"].strip()]
    print(
        f"manifest={state.manifest_name} items={len(state.items)} "
        f"images={len(state.image_paths)} already_annotated={state.store.annotated_count()}"
    )
    print(f"images_root={state.images_root}")
    print(f"translations_loaded={len(state.translations)}")
    if empty_queries:
        print(f"WARNING: {len(empty_queries)} items have empty queries", file=sys.stderr)
    print(f"serving: http://{display_host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
