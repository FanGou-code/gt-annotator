import base64
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server import create_server
import server as server_module

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
BOX = [0.1, 0.2, 0.3, 0.4]


def request(method: str, url: str, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        content_type = exc.headers.get("Content-Type", "")
        status = exc.code
    else:
        status = 200
    if "application/json" in content_type:
        return status, json.loads(body.decode("utf-8"))
    return status, body


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.images_root = self.root / "images"
        self.images_root.mkdir()
        (self.images_root / "a.png").write_bytes(PNG_1PX)
        (self.images_root / "b.png").write_bytes(PNG_1PX)
        manifest = {
            "name": "fixture",
            "images_root": str(self.images_root),
            "items": [
                {"id": "000001_001", "image": "a.png", "query": "the red square"},
                {"id": "000001_002", "image": "b.png", "query": "the blue circle"},
            ],
        }
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.data_dir = self.root / "annotations"

    def start(self, token: str = ""):
        server, state = create_server(
            manifest_path=self.manifest_path,
            data_dir=self.data_dir,
            host="127.0.0.1",
            port=0,
            token=token,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_stop)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        return base, state

    def test_session_shape(self):
        base, state = self.start()
        status, payload = request("GET", f"{base}/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(payload["manifest"], "fixture")
        self.assertEqual(payload["total_items"], 2)
        self.assertEqual(payload["annotated"], 0)
        first = payload["items"][0]
        self.assertEqual(first["id"], "000001_001")
        self.assertEqual(first["query_en"], "the red square")
        self.assertIsNone(first["query_zh"])
        self.assertIsNone(first["bbox"])
        self.assertTrue(first["image_url"].startswith("/image?src="))

    def test_image_serving_and_rejection(self):
        base, _ = self.start()
        status, body = request("GET", f"{base}/image?src=a.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_1PX)
        status, _ = request("GET", f"{base}/image?src=missing.png")
        self.assertEqual(status, 404)
        status, _ = request("GET", f"{base}/image?src=../manifest.json")
        self.assertEqual(status, 404)
        status, _ = request("GET", f"{base}/image")
        self.assertEqual(status, 404)

    def test_put_get_delete_cycle(self):
        base, state = self.start()
        status, payload = request(
            "PUT",
            f"{base}/api/item/000001_001/bbox",
            payload={"bbox": BOX, "annotator": "alice"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["bbox"], BOX)
        self.assertTrue(payload["annotated"])
        status, payload = request("GET", f"{base}/api/progress")
        self.assertEqual(status, 200)
        self.assertEqual(payload["annotated"], 1)
        self.assertEqual(payload["total_items"], 2)
        self.assertEqual(payload["annotated_images"], 1)
        status, payload = request("DELETE", f"{base}/api/item/000001_001/bbox")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["bbox"])
        self.assertEqual(state.store.annotated_count(), 0)

    def test_put_validates_bbox_and_ids(self):
        base, _ = self.start()
        status, _ = request(
            "PUT", f"{base}/api/item/000001_001/bbox", payload={"bbox": [0.5, 0.5, 0.4, 0.6]}
        )
        self.assertEqual(status, 400)
        status, _ = request(
            "PUT", f"{base}/api/item/000001_001/bbox", payload={"bbox": [0, 0, 0, 0]}
        )
        self.assertEqual(status, 400)
        status, _ = request("PUT", f"{base}/api/item/000001_001/bbox", payload={})
        self.assertEqual(status, 400)
        status, _ = request(
            "PUT", f"{base}/api/item/999999_999/bbox", payload={"bbox": BOX}
        )
        self.assertEqual(status, 404)
        status, _ = request("DELETE", f"{base}/api/item/999999_999/bbox")
        self.assertEqual(status, 404)

    def test_token_auth(self):
        base, _ = self.start(token="s3cret")
        status, _ = request("GET", f"{base}/api/session")
        self.assertEqual(status, 401)
        status, _ = request("GET", f"{base}/api/session", headers={"X-Auth-Token": "wrong"})
        self.assertEqual(status, 401)
        status, _ = request("GET", f"{base}/api/session", headers={"X-Auth-Token": "s3cret"})
        self.assertEqual(status, 200)

    def test_static_served_without_token_api_stays_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = Path(tmp)
            (web / "index.html").write_text("<html>ok</html>", encoding="utf-8")
            original = server_module.WEB_ROOT
            server_module.WEB_ROOT = web
            try:
                base, _ = self.start(token="s3cret")
                status, body = request("GET", f"{base}/")
                self.assertEqual(status, 200)
                self.assertIn(b"ok", body)
                status, _ = request("GET", f"{base}/api/session")
                self.assertEqual(status, 401)
            finally:
                server_module.WEB_ROOT = original

    def test_static_missing_frontend_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = server_module.WEB_ROOT
            server_module.WEB_ROOT = Path(tmp)
            try:
                base, _ = self.start()
                status, payload = request("GET", f"{base}/")
                self.assertEqual(status, 404)
                self.assertIn("error", payload)
            finally:
                server_module.WEB_ROOT = original

    def test_state_resumes_across_restart(self):
        base, _ = self.start()
        status, _ = request(
            "PUT", f"{base}/api/item/000001_002/bbox", payload={"bbox": BOX}
        )
        self.assertEqual(status, 200)
        base2, state2 = self.start()
        status, payload = request("GET", f"{base2}/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(payload["annotated"], 1)
        second = payload["items"][1]
        self.assertEqual(second["bbox"], BOX)
        self.assertEqual(second["annotator"], None)
        self.assertEqual(state2.store.annotated_count(), 1)


if __name__ == "__main__":
    unittest.main()
