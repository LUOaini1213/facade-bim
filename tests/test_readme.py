"""README 里的每个数字都能从产物回算（scripts/check_readme.py）。"""
import glob
import os
import re
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


def structure_table_paths(readme):
    """README「仓库结构」表第一列里的路径。"""
    section = readme.split("## 仓库结构", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| `([^`]+)` \|", section, flags=re.M)


class StructureTable(unittest.TestCase):
    """仓库结构表列出每个入口脚本，且不列已不存在的路径。"""

    def setUp(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            self.listed = structure_table_paths(f.read())

    def test_every_entry_point_is_listed(self):
        scripts = sorted(os.path.relpath(p, ROOT).replace(os.sep, "/")
                         for d in ("facade", "rhino", "scripts") for p in glob.glob(os.path.join(ROOT, d, "*.py"))
                         if os.path.basename(p) != "__init__.py")
        self.assertGreater(len(scripts), 20)
        missing = [s for s in scripts if s not in self.listed]
        self.assertEqual(missing, [], "README 仓库结构表缺这些脚本")

    def test_every_listed_path_exists(self):
        stale = [p for p in self.listed if not os.path.exists(os.path.join(ROOT, p))]
        self.assertEqual(stale, [], "README 仓库结构表列了不存在的路径")


if __name__ == "__main__":
    unittest.main()
