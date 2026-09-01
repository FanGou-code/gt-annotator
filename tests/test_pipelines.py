import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from make_manifest import build_items, check_images
from translate import load_journal, write_snapshot

DICT_SOURCE = {
    "000001_001": {"visible": "Images/visible/a.jpg", "query": "the red square"},
    "000001_002": {"visible": "Images/visible/b.jpg", "query": "the blue circle"},
}
LIST_SOURCE = [
    {"id": "x1", "image": "a.png", "query": "leftmost person"},
    {"id": "x2", "image": "b.png", "query": "tallest man"},
]


class BuildItemsTests(unittest.TestCase):
    def test_mapping_shape_takes_id_from_key(self):
        items = build_items(DICT_SOURCE, "visible", "query")
        self.assertEqual([i["id"] for i in items], ["000001_001", "000001_002"])
        self.assertEqual(items[0]["image"], "Images/visible/a.jpg")

    def test_list_shape(self):
        items = build_items(LIST_SOURCE, "image", "query")
        self.assertEqual([i["id"] for i in items], ["x1", "x2"])

    def test_alternate_field_names(self):
        source = {"k1": {"rgb": "a.jpg", "text": "a query"}}
        items = build_items(source, "rgb", "text")
        self.assertEqual(items[0]["image"], "a.jpg")
        self.assertEqual(items[0]["query"], "a query")

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(ValueError):
            build_items([LIST_SOURCE[0], dict(LIST_SOURCE[0])], "image", "query")

    def test_rejects_backslash_paths(self):
        with self.assertRaises(ValueError):
            build_items({"k1": {"visible": "Images\\a.jpg", "query": "q"}}, "visible", "query")

    def test_rejects_missing_fields(self):
        with self.assertRaises(ValueError):
            build_items({"k1": {"visible": "a.jpg"}}, "visible", "query")
        with self.assertRaises(ValueError):
            build_items({"k1": {"visible": "a.jpg", "query": "   "}}, "visible", "query")

    def test_rejects_non_string_mapping_key(self):
        with self.assertRaises(ValueError):
            build_items({7: {"visible": "a.jpg", "query": "q"}}, "visible", "query")

    def test_check_images_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.jpg").write_bytes(b"x")
            items = [
                {"id": "1", "image": "a.jpg", "query": "q"},
                {"id": "2", "image": "nope.jpg", "query": "q"},
            ]
            self.assertEqual(check_images(items, root), ["nope.jpg"])


class TranslateJournalTests(unittest.TestCase):
    def test_journal_roundtrip_and_torn_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "translations.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": "a", "zh": "红色的方块"}, ensure_ascii=False) + "\n")
                fh.write('{"id": "b", "zh": "蓝色')  # torn write
            self.assertEqual(load_journal(path), {"a": "红色的方块"})

    def test_snapshot_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "translations.json"
            write_snapshot({"a": "红色"}, out)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"a": "红色"})
            self.assertFalse(out.with_name(out.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
