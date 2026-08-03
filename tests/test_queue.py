import tempfile
import unittest
from pathlib import Path

from videocrush_queue import JobQueue, QueueJob, QueueStore


class QueueTests(unittest.TestCase):
    def test_priority_selects_highest_pending_without_reordering_display_order(self):
        queue = JobQueue()
        first = queue.add(QueueJob("first.mp4", "first_out.mp4", priority=0))
        second = queue.add(QueueJob("second.mp4", "second_out.mp4", priority=10))
        self.assertEqual(queue.jobs, [first, second])
        self.assertIs(queue.next_pending(), second)

    def test_move_and_retry(self):
        queue = JobQueue([
            QueueJob("first.mp4", "first_out.mp4"),
            QueueJob("second.mp4", "second_out.mp4", state="failed", error="bad input"),
        ])
        queue.move(queue.jobs[1].id, -1)
        self.assertEqual(queue.jobs[0].name, "second.mp4")
        queue.retry(queue.jobs[0].id)
        self.assertEqual(queue.jobs[0].state, "pending")
        self.assertEqual(queue.jobs[0].error, "")

    def test_overrides_and_logs_survive_atomic_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            queue = JobQueue()
            job = queue.add(
                QueueJob(
                    "input.mov",
                    "output.mp4",
                    overrides={"crf": 26, "resolution": "720p"},
                    priority=3,
                )
            )
            job.append_log("ffmpeg stderr")
            QueueStore(path).save(queue)
            loaded = QueueStore(path).load()
            self.assertEqual(loaded.jobs[0].overrides["crf"], 26)
            self.assertEqual(loaded.jobs[0].logs, ["ffmpeg stderr"])
            self.assertEqual(loaded.jobs[0].priority, 3)


if __name__ == "__main__":
    unittest.main()
