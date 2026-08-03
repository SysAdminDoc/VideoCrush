import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from videocrush_cli import _process_items


class CliTests(unittest.TestCase):
    def test_batch_items_use_requested_worker_count(self):
        args = SimpleNamespace(workers=2)
        jobs = [(Path("one.mp4"), "web-1080p"), (Path("two.mp4"), "web-720p")]

        with patch("videocrush_cli._process_item", side_effect=[True, False]) as process:
            results = _process_items(args, jobs, Path("out"), {}, multiple=True)

        self.assertEqual(results, [(jobs[0][0], True), (jobs[1][0], False)])
        self.assertEqual(process.call_count, 2)


if __name__ == "__main__":
    unittest.main()
