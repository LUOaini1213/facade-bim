"""README 里的每个数字都能从产物回算（scripts/check_readme.py）。"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Readme(unittest.TestCase):
    def test_every_number_in_readme_matches_the_artifacts(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_readme.py")],
                           capture_output=True, text=True, encoding="utf-8",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
