"""Batch-translate manifest queries via a Zhipu OpenAI-compatible endpoint.

Resume: per-record JSONL journal next to --out; already-translated ids are
skipped on rerun. Snapshot ``{item_id: chinese}`` is written atomically to
--out at the end and on Ctrl+C. Terminal status lines use absolute timestamps.

Usage::

    API_KEY=... python translate.py --manifest data/manifest.json \
        --out data/translations.json --concurrency 8 [--limit 50]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from server import load_manifest

DEFAULT_MODEL = "glm-4.5-air"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_CONCURRENCY = 8
MAX_ATTEMPTS = 4
JOURNAL_NAME = "translations.jsonl"
LOG_EVERY = 25
SYSTEM_PROMPT = (
    "You are a professional translator for visual grounding referring expressions. "
    "Translate the English query into natural Chinese. Preserve the meaning exactly, "
    "including counts, ordinals (first/second/leftmost/farthest...), spatial "
    "relations, and object categories. Output ONLY the Chinese translation: no "
    "pinyin, no quotes, no explanations."
)


def _log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def translate_once(
    base_url: str, model: str, api_key: str, text: str, timeout: float = 60.0
) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "thinking": {"type": "disabled"},
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
        raise ValueError("empty completion content")
    return content.strip()


def load_journal(path: Path) -> dict[str, str]:
    done: dict[str, str] = {}
    if not path.is_file():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = record.get("id")
            zh = record.get("zh")
            if isinstance(item_id, str) and isinstance(zh, str) and zh.strip():
                done[item_id] = zh
    return done


def write_snapshot(done: dict[str, str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(
        json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(out_path)


class Progress:
    def __init__(self, total: int) -> None:
        self.total = total
        self.finished = 0
        self.ok = 0
        self.failed = 0
        self._started = time.monotonic()
        self._lock = threading.Lock()

    def record(self, ok: bool) -> None:
        with self._lock:
            self.finished += 1
            if ok:
                self.ok += 1
            else:
                self.failed += 1
            should_log = self.finished % LOG_EVERY == 0 or self.finished == self.total
            finished, ok, failed = self.finished, self.ok, self.failed
        if should_log:
            elapsed = max(time.monotonic() - self._started, 1e-6)
            rate = finished / elapsed
            remaining = (self.total - finished) / max(rate, 1e-6)
            _log(
                f"{finished}/{self.total} ok:{ok} retry-fail:{failed} "
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

    _, _, items = load_manifest(Path(args.manifest).expanduser())
    out_path = Path(args.out).expanduser()
    journal_path = out_path.parent / JOURNAL_NAME
    done = load_journal(journal_path)
    todo = [(item["id"], item["query"]) for item in items if item["id"] not in done]
    if args.limit is not None:
        todo = todo[: args.limit]
    _log(
        f"model={args.model} items={len(items)} journal_done={len(done)} todo={len(todo)}"
    )
    if not todo:
        write_snapshot(done, out_path)
        _log(f"nothing to do; snapshot at {out_path}")
        return 0

    progress = Progress(len(todo))
    journal_lock = threading.Lock()

    def translate_one(task: tuple[str, str]) -> None:
        item_id, query = task
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                zh = translate_once(
                    args.base_url, args.model, api_key, query, timeout=args.timeout
                )
                break
            except (HTTPError, URLError, ValueError, TimeoutError, OSError) as exc:
                if attempt == MAX_ATTEMPTS:
                    progress.record(False)
                    _log(f"FAIL {item_id}: {exc}")
                    return
                time.sleep(min(2 ** (attempt - 1), 8) + random.random() * 0.5)
        with journal_lock:
            done[item_id] = zh
            with journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": item_id, "zh": zh}, ensure_ascii=False) + "\n")
                fh.flush()
        if args.verbose:
            _log(f"{item_id}: {query} -> {zh}")
        progress.record(True)

    exit_code = 0
    _log(f"translating {len(todo)} queries with {args.concurrency} workers")
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(translate_one, task) for task in todo]
            for future in as_completed(futures):
                future.result()
    except KeyboardInterrupt:
        exit_code = 130
        _log("interrupted; journal kept, snapshot written")
    finally:
        write_snapshot(done, out_path)
    _log(
        f"done ok:{progress.ok} retry-fail:{progress.failed} "
        f"snapshot={out_path} journal={journal_path}"
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="batch-translate manifest queries")
    parser.add_argument("--manifest", required=True, help="manifest.json path")
    parser.add_argument("--out", required=True, help="translations snapshot path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--limit", type=int, default=None, help="translate first N only")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="print each finished translation"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-request read timeout in seconds (default 60)",
    )
    args = parser.parse_args(argv)
    if args.concurrency < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--concurrency must be >= 1 and --limit must be >= 1")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
