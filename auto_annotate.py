"""Batch-annotate manifest queries via GLM-4.6V (Zhipu Vision-Language Model).

Features:
  - Strict Prompt Constraint: integer [0, 1000] coordinates [xmin, ymin, xmax, ymax].
  - Negative Rejection: detects when query target does not exist, marks as absent.
  - Non-destructive: strictly skips items already annotated (e.g. human annotations).
  - Provenance: tags generated boxes with annotator="glm-4.6v".
  - Standard library only: pure Python stdlib, zero extra pip dependencies.

Usage::

    API_KEY=... python auto_annotate.py --manifest data/manifest.json \
        --data-dir data --model glm-4.6v --concurrency 4 [--limit 10]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bbox import normalize_bbox
from server import load_manifest
from store import AnnotationStore

DEFAULT_MODEL = "glm-4.6v"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_CONCURRENCY = 4
MAX_ATTEMPTS = 6
JOURNAL_NAME = "annotations.jsonl"
LOG_EVERY = 10

SYSTEM_PROMPT = (
    "You are an expert visual grounding model.\n"
    "Given an image and a referring expression query, locate the described target object in the image.\n\n"
    "Rules:\n"
    "1. Coordinates: Output normalized [0, 1000] integer coordinates [xmin, ymin, xmax, ymax] "
    "relative to image width and height. Ensure xmin < xmax and ymin < ymax.\n"
    "2. Strict Rejection: If the target object does NOT exist in the image, or the query describes "
    "something absent, do NOT hallucinate or select unrelated objects. "
    "Set exists to false and box_1000 to null.\n"
    "3. Output Format: Output ONLY a single JSON object. No explanations, no markdown code blocks.\n"
    'When object exists: {"exists": true, "box_1000": [xmin, ymin, xmax, ymax]}\n'
    'When object does not exist: {"exists": false, "box_1000": null}'
)


def _log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def encode_image_to_data_url(path: Path) -> str:
    """Encode local image file to base64 Data URL."""
    ext = path.suffix.lower().lstrip(".")
    mime = "image/png" if ext == "png" else "image/jpeg"
    with path.open("rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def parse_vlm_response(content: str) -> tuple[bool, list[float] | None]:
    """Parse model response into (exists, normalized_bbox)."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON from VLM: {content[:120]}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data)}")

    exists = bool(data.get("exists", False))
    box_1000 = data.get("box_1000")

    if not exists or box_1000 is None:
        return False, None

    if not isinstance(box_1000, list) or len(box_1000) != 4:
        raise ValueError(f"box_1000 must be list of 4 integers, got {box_1000}")

    try:
        norm = [float(v) / 1000.0 for v in box_1000]
        clamped = normalize_bbox(norm)
        return True, clamped
    except ValueError as exc:
        raise ValueError(f"invalid box coordinates {box_1000}: {exc}") from exc


def call_glm_once(
    base_url: str,
    model: str,
    api_key: str,
    image_data_url: str,
    query: str,
    timeout: float = 60.0,
) -> tuple[bool, list[float] | None]:
    """Call GLM multimodal completions endpoint once."""
    user_content = [
        {"type": "image_url", "image_url": {"url": image_data_url}},
        {"type": "text", "text": query},
    ]

    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
    ).encode("utf-8")

    request = Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urlopen(request, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    choices = payload.get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty completion content from GLM API")

    return parse_vlm_response(content)


def load_processed_ids(journal_path: Path) -> set[str]:
    """Scan annotations.jsonl to find items that have already been annotated or marked absent."""
    done: set[str] = set()
    if not journal_path.is_file():
        return done

    with journal_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = record.get("id")
            if not item_id:
                continue
            if record.get("bbox") is not None:
                done.add(item_id)
            elif str(record.get("annotator") or "").endswith(":absent"):
                done.add(item_id)
    return done


class Progress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.finished = 0
        self.found = 0
        self.absent = 0
        self.failed = 0
        self._started = time.monotonic()
        self._lock = threading.Lock()

    def record(self, status: str) -> None:
        with self._lock:
            self.finished += 1
            if status == "found":
                self.found += 1
            elif status == "absent":
                self.absent += 1
            else:
                self.failed += 1
            should_log = self.finished % LOG_EVERY == 0 or self.finished == self.total
            finished, found, absent, failed = (
                self.finished,
                self.found,
                self.absent,
                self.failed,
            )
        if should_log:
            elapsed = max(time.monotonic() - self._started, 1e-6)
            rate = finished / elapsed
            remaining = (self.total - finished) / max(rate, 1e-6)
            _log(
                f"{finished}/{self.total} found:{found} absent:{absent} fail:{failed} "
                f"| {rate:.1f}/s | ETA {remaining / 60:.1f}min"
            )


def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print(
            "API_KEY is not set. Set it in the current environment; "
            "never store it in the repository.",
            file=sys.stderr,
        )
        return 2

    manifest_name, images_root, items = load_manifest(Path(args.manifest).expanduser())
    data_dir = Path(args.data_dir).expanduser()
    store = AnnotationStore(data_dir)
    journal_path = data_dir / JOURNAL_NAME

    # Check already processed items to strictly avoid overwriting existing annotations
    done_ids = load_processed_ids(journal_path)
    todo = [item for item in items if item["id"] not in done_ids]

    if args.limit is not None:
        todo = todo[: args.limit]

    _log(
        f"manifest={manifest_name} total_items={len(items)} "
        f"already_done={len(done_ids)} todo={len(todo)} model={args.model}"
    )

    if not todo:
        _log("nothing to do; all items in scope already have annotations!")
        return 0

    progress = Progress(len(todo))
    store_lock = threading.Lock()

    def annotate_one(item: dict) -> None:
        item_id = item["id"]
        query = item.get("query") or item.get("query_en") or ""
        img_rel = item.get("image") or ""
        img_path = images_root / img_rel

        if not img_path.is_file():
            _log(f"ERROR {item_id}: image not found at {img_path}")
            progress.record("failed")
            return

        try:
            data_url = encode_image_to_data_url(img_path)
        except Exception as exc:
            _log(f"ERROR {item_id}: failed to encode image {img_path}: {exc}")
            progress.record("failed")
            return

        exists = False
        bbox = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                exists, bbox = call_glm_once(
                    args.base_url,
                    args.model,
                    api_key,
                    data_url,
                    query,
                    timeout=args.timeout,
                )
                break
            except (HTTPError, URLError, ValueError, TimeoutError, OSError) as exc:
                detail = ""
                if isinstance(exc, HTTPError):
                    try:
                        detail = exc.read(512).decode("utf-8", errors="replace")
                    except Exception:
                        pass
                if attempt == MAX_ATTEMPTS:
                    progress.record("failed")
                    _log(f"FAIL {item_id}: {exc} {detail}".strip())
                    return
                is_429 = isinstance(exc, HTTPError) and exc.code == 429
                if is_429:
                    retry_after = exc.headers.get("Retry-After") if hasattr(exc, "headers") and exc.headers else None
                    try:
                        parsed_delay = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        parsed_delay = 0.0
                    sleep_time = parsed_delay if parsed_delay > 0.0 else min(2.0 * (2 ** (attempt - 1)), 15.0) + random.random()
                    _log(f"Rate limited (429) on {item_id}, cooling down {sleep_time:.1f}s (retry {attempt}/{MAX_ATTEMPTS})...")
                else:
                    sleep_time = min(2 ** (attempt - 1), 8) + random.random() * 0.5
                time.sleep(sleep_time)

        with store_lock:
            if exists and bbox is not None:
                store.set(item_id, bbox, annotator=args.model)
                status_str = "found"
            else:
                absent_record = {
                    "id": item_id,
                    "bbox": None,
                    "annotator": f"{args.model}:absent",
                    "ts": _now(),
                }
                with journal_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(absent_record, ensure_ascii=False) + "\n")
                    fh.flush()
                status_str = "absent"

        if args.verbose:
            if exists and bbox:
                _log(f"{item_id} [BBOX] {bbox}")
            else:
                _log(f"{item_id} [ABSENT]")

        progress.record(status_str)

    exit_code = 0
    _log(f"annotating {len(todo)} items with {args.concurrency} workers...")
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(annotate_one, item) for item in todo]
            for future in as_completed(futures):
                future.result()
    except KeyboardInterrupt:
        exit_code = 130
        _log("interrupted by user; progress kept safely in journal and snapshot")
    finally:
        _log(
            f"finished: found={progress.found} absent={progress.absent} "
            f"failed={progress.failed} / total={progress.total}"
        )

    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Batch visual grounding annotation worker via GLM-4.6V"
    )
    parser.add_argument("--manifest", required=True, help="manifest.json path")
    parser.add_argument("--data-dir", default="data", help="data directory (default data)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"model name (default {DEFAULT_MODEL})")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible API base URL")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="concurrent worker threads")
    parser.add_argument("--limit", type=int, default=None, help="only process first N todo items")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout in seconds")
    parser.add_argument("--verbose", "-v", action="store_true", help="print each prediction in terminal")
    args = parser.parse_args(argv)

    if args.concurrency < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--concurrency and --limit must be >= 1")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
