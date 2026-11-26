import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from woof import translate


def rec(kind, version="1.0", frm="en", to="zh-Hant", filt=None, name=None):
    return {"fromLang": frm, "toLang": to, "fileType": kind, "version": version, "filter_expression": filt,
            "name": name or f"{kind}.{version}", "attachment": {}}


class PickTest(unittest.TestCase):
    def setUp(self):
        self.wasm = [{"name": "bergamot-translator", "version": "2.0"}, {"name": "bergamot-translator", "version": "3.0", "id": "w3"}]

    def test_latest_complete_version(self):
        kinds = ("model", "lex", "srcvocab", "trgvocab")
        models = [rec(k, "1.0") for k in kinds] + [rec(k, "2.0") for k in kinds] + [rec("model", "3.0")]
        got = translate.pick(models, self.wasm)
        self.assertEqual({got[k]["version"] for k in kinds}, {"2.0"})   # 3.0 缺檔不選
        self.assertEqual(got["wasm"]["id"], "w3")

    def test_skips_other_pairs_and_filtered(self):
        kinds = ("model", "lex", "srcvocab", "trgvocab")
        models = [rec(k, "1.0") for k in kinds] + [rec(k, "9.0", filt="env.channel == 'nightly'") for k in kinds] \
            + [rec(k, "9.0", to="zh-Hans") for k in kinds]
        self.assertEqual(translate.pick(models, self.wasm)["model"]["version"], "1.0")

    def test_alpha_older_than_release(self):
        kinds = ("model", "lex", "srcvocab", "trgvocab")
        models = [rec(k, "2.0a2") for k in kinds] + [rec(k, "2.0") for k in kinds]
        self.assertEqual(translate.pick(models, self.wasm)["model"]["version"], "2.0")

    def test_missing_raises(self):
        with self.assertRaises(RuntimeError):
            translate.pick([], self.wasm)


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.p = mock.patch.object(translate, "DIR", self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_not_ready_without_manifest(self):
        self.assertFalse(translate.ready())
        self.assertIsNone(translate.path_of("model"))

    def test_ready_and_path(self):
        names = {k: f"{k}.bin" for k in translate.KINDS}
        for n in names.values():
            (self.tmp / n).write_bytes(b"x")
        (self.tmp / "manifest.json").write_text(json.dumps({"files": names}), encoding="utf-8")
        self.assertTrue(translate.ready())
        self.assertEqual(translate.path_of("lex"), self.tmp / "lex.bin")
        self.assertIsNone(translate.path_of("../secret"))
        self.assertEqual(translate.status()["bytes"], 5)
        translate.remove()
        self.assertFalse(translate.ready())

    def test_hash_mismatch_is_error(self):
        data = b"model-bytes"
        models = [dict(rec(k), attachment={"filename": f"{k}.bin", "size": len(data), "location": k,
                                           "hash": hashlib.sha256(data if k != "lex" else b"other").hexdigest()})
                  for k in ("model", "lex", "srcvocab", "trgvocab")]
        wasm = [{"name": "bergamot-translator", "version": "3.0",
                 "attachment": {"filename": "w.wasm", "size": len(data), "location": "w", "hash": hashlib.sha256(data).hexdigest()}}]

        class Resp:
            def __init__(self, payload=None):
                self.payload = payload

            def raise_for_status(self):
                return self

            def json(self):
                return {"data": self.payload}

            def iter_bytes(self, n):
                yield data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return Resp(models if "models" in url else wasm)

            def stream(self, method, url):
                return Resp()

        with mock.patch.object(translate.httpx, "Client", Client):
            translate.download()
        st = translate.status()
        self.assertEqual(st["job"]["state"], "error")
        self.assertIn("核對不符", st["job"]["error"])
        self.assertFalse(translate.ready())
        self.assertFalse(any(f.suffix == ".part" for f in self.tmp.iterdir()))


if __name__ == "__main__":
    unittest.main()
