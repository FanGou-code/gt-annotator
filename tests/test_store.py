import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from store import ABSENT_SNAPSHOT_NAME, SNAPSHOT_NAME, AnnotationStore

BOX_A = [0.1, 0.2, 0.3, 0.4]
BOX_B = [0.5, 0.5, 0.6, 0.7]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_set_and_get(self):
        store = AnnotationStore(self.data_dir)
        saved = store.set("id1", BOX_A, annotator="alice")
        self.assertEqual(saved, BOX_A)
        self.assertEqual(store.get("id1"), BOX_A)
        self.assertEqual(store.meta("id1")["annotator"], "alice")
        self.assertEqual(store.annotated_count(), 1)

    def test_snapshot_matches_prediction_format(self):
        store = AnnotationStore(self.data_dir)
        store.set("id1", BOX_A)
        snapshot = json.loads((self.data_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(snapshot, {"id1": BOX_A})

    def test_delete_removes_from_state_and_snapshot(self):
        store = AnnotationStore(self.data_dir)
        store.set("id1", BOX_A)
        store.delete("id1")
        self.assertIsNone(store.get("id1"))
        self.assertEqual(store.annotated_count(), 0)
        snapshot = json.loads((self.data_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(snapshot, {})

    def test_last_write_wins(self):
        store = AnnotationStore(self.data_dir)
        store.set("id1", BOX_A)
        store.set("id1", BOX_B)
        self.assertEqual(store.get("id1"), BOX_B)

    def test_resume_from_journal(self):
        store = AnnotationStore(self.data_dir)
        store.set("id1", BOX_A, annotator="alice")
        store.set("id2", BOX_B)
        store.delete("id2")
        reopened = AnnotationStore(self.data_dir)
        self.assertEqual(reopened.get("id1"), BOX_A)
        self.assertIsNone(reopened.get("id2"))
        self.assertEqual(reopened.meta("id1")["annotator"], "alice")

    def test_absent_record_survives_replay(self):
        store = AnnotationStore(self.data_dir)
        with store.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "a1", "bbox": None, "annotator": "glm-4.6v:absent", "ts": "t"}) + "\n")
        reopened = AnnotationStore(self.data_dir)
        self.assertIsNone(reopened.get("a1"))
        self.assertEqual(reopened.meta("a1")["annotator"], "glm-4.6v:absent")
        self.assertEqual(reopened.absent_items(), {"a1": "glm-4.6v:absent"})

    def test_plain_delete_clears_absent_state(self):
        store = AnnotationStore(self.data_dir)
        store.set_absent("a1", "fang0")
        with store.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": "a1", "bbox": None, "annotator": None, "ts": "t"}) + "\n")
        reopened = AnnotationStore(self.data_dir)
        self.assertIsNone(reopened.get("a1"))
        self.assertIsNone(reopened.meta("a1"))
        self.assertEqual(reopened.absent_items(), {})

    def test_set_absent_writes_journal_and_snapshot(self):
        store = AnnotationStore(self.data_dir)
        record = store.set_absent("a1", "fang0")
        self.assertEqual(record, {"id": "a1", "bbox": None, "annotator": "fang0:absent"})
        self.assertIsNone(store.get("a1"))
        self.assertEqual(store.meta("a1")["annotator"], "fang0:absent")
        self.assertEqual(store.absent_items(), {"a1": "fang0:absent"})
        absent_snapshot = json.loads(
            (self.data_dir / ABSENT_SNAPSHOT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(absent_snapshot, {"a1": "fang0:absent"})
        reopened = AnnotationStore(self.data_dir)
        self.assertEqual(reopened.absent_items(), {"a1": "fang0:absent"})
        self.assertEqual(reopened.meta("a1")["annotator"], "fang0:absent")

    def test_box_and_absent_overwrite_each_other(self):
        store = AnnotationStore(self.data_dir)
        store.set_absent("a1", "fang0")
        store.set("a1", BOX_A, annotator="fang0")
        self.assertEqual(store.get("a1"), BOX_A)
        self.assertEqual(store.absent_items(), {})
        store.set_absent("a1", "fang0")
        self.assertIsNone(store.get("a1"))
        self.assertEqual(store.absent_items(), {"a1": "fang0:absent"})
        snapshot = json.loads((self.data_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(snapshot, {})

    def test_delete_reverts_absent(self):
        store = AnnotationStore(self.data_dir)
        store.set_absent("a1", "fang0")
        store.delete("a1")
        self.assertEqual(store.absent_items(), {})
        self.assertIsNone(store.meta("a1"))
        absent_snapshot = json.loads(
            (self.data_dir / ABSENT_SNAPSHOT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(absent_snapshot, {})

    def test_snapshot_keeps_entries_written_by_other_store_instance(self):
        # Regression: snapshots must be re-derived from the journal, otherwise
        # concurrent writers (batch annotator vs server) drop each other's boxes.
        store_a = AnnotationStore(self.data_dir)
        store_b = AnnotationStore(self.data_dir)
        store_b.set("b1", BOX_B, annotator="bob")
        store_a.set("a1", BOX_A, annotator="alice")
        snapshot = json.loads((self.data_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(snapshot, {"a1": BOX_A, "b1": BOX_B})

    def test_reader_hot_reloads_external_journal_appends(self):
        store_a = AnnotationStore(self.data_dir)
        store_b = AnnotationStore(self.data_dir)
        store_b.set("b1", BOX_B, annotator="bob")
        store_b.set_absent("b2", "bob")
        self.assertEqual(store_a.get("b1"), BOX_B)
        self.assertEqual(store_a.absent_items(), {"b2": "bob:absent"})
        self.assertEqual(store_a.annotated_count(), 1)

    def test_torn_tail_line_is_ignored(self):
        store = AnnotationStore(self.data_dir)
        store.set("id1", BOX_A)
        with store.journal_path.open("a", encoding="utf-8") as fh:
            fh.write('{"id": "id9", "bbox": [0.1, 0.1, 0.2, 0.2"')  # torn write
        reopened = AnnotationStore(self.data_dir)
        self.assertEqual(reopened.get("id1"), BOX_A)
        self.assertIsNone(reopened.get("id9"))

    def test_concurrent_sets_are_thread_safe(self):
        store = AnnotationStore(self.data_dir)
        errors = []

        def worker(worker_id: int) -> None:
            try:
                for i in range(20):
                    store.set(
                        f"id{worker_id}_{i}",
                        [0.01 * worker_id, 0.1, 0.2, 0.2],
                    )
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(store.annotated_count(), 160)
        snapshot = json.loads((self.data_dir / SNAPSHOT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(snapshot), 160)


if __name__ == "__main__":
    unittest.main()
