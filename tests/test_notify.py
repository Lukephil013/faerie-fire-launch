"""Desktop notifications (livingpc/notify.py)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import Config
from livingpc.notify import import_summary, notify, review_reminder


class TestNotify(unittest.TestCase):
    def test_disabled_by_config(self):
        cfg = Config()
        cfg.notifications_enabled = False
        self.assertFalse(notify("t", "m", cfg=cfg))

    def test_best_effort_never_raises(self):
        # On non-Windows this returns False; on Windows it dispatches. Either
        # way it must not raise, even with awkward input.
        cfg = Config()
        cfg.notifications_enabled = False
        self.assertFalse(notify("x" * 500, None, cfg=cfg))

    def test_review_reminder_wording(self):
        title, body = review_reminder(1)
        self.assertIn("1 inference ready", title)
        title, _ = review_reminder(3)
        self.assertIn("3 inferences", title)
        self.assertIn("Memory GUI", body)

    def test_import_summary_wording(self):
        title, body = import_summary({"added": 5, "batches": 2}, dry_run=True)
        self.assertIn("import finished", title)
        self.assertNotIn("dry run", title.lower())
        title, body = import_summary({"added": 7, "superseded": 2}, dry_run=False)
        self.assertIn("import finished", title)
        self.assertIn("+7", body)
        self.assertIn("Timeline", body)


if __name__ == "__main__":
    unittest.main()
