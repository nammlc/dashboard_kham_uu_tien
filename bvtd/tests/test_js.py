"""Chạy test JavaScript của pop-up bằng Node (bỏ qua nếu máy không có node)."""
import os
import shutil
import subprocess
import unittest

from tests.helpers import ROOT


@unittest.skipUnless(shutil.which("node"), "cần Node.js để chạy test JavaScript")
class PopupJs(unittest.TestCase):
    def test_popup_behaviour(self):
        r = subprocess.run(["node", os.path.join(ROOT, "tests", "js", "popup.test.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ALL JS OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
