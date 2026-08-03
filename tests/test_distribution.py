import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import videocrush_core
from videocrush_distribution import check_for_update
from videocrush_queue import default_queue_path


class DistributionTests(unittest.TestCase):
    def test_release_check_normalizes_tags_and_compares_versions(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def opener(_request, timeout):
            self.assertEqual(timeout, 3)
            return Response(json.dumps({"tag_name": "v0.2.0", "html_url": "https://example.test/release"}).encode())

        result = check_for_update(current_version="0.1.0", timeout=3, opener=opener)
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest_version, "0.2.0")

    def test_portable_queue_path_is_local(self):
        with tempfile.TemporaryDirectory():
            with patch.dict("os.environ", {"VIDEOCRUSH_PORTABLE": "1"}, clear=False):
                path = default_queue_path()
            self.assertEqual(path.name, "queue.json")
            self.assertEqual(path.parent.name, "data")
            self.assertTrue(Path(path).is_absolute())

    def test_frozen_build_prefers_bundled_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            bundled = Path(directory) / "ffmpeg.exe"
            bundled.write_bytes(b"stub")
            with patch.object(videocrush_core.sys, "_MEIPASS", directory, create=True):
                with patch.object(videocrush_core.sys, "frozen", True, create=True):
                    self.assertEqual(Path(videocrush_core.find_ffmpeg()), bundled)


if __name__ == "__main__":
    unittest.main()
