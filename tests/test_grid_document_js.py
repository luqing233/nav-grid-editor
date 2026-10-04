# -*- coding: utf-8 -*-
"""前端分块网格文档的独立回归测试。"""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src" / "nav_grid_editor" / "web" / "static" / "js" / "grid" / "grid-document.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend model tests")
class GridDocumentJsTest(unittest.TestCase):
    def test_transactions_history_bbox_export_and_rebase(self):
        script = f"""
import {{ GridDocument, CELL_FREE, CELL_BLOCKED, CELL_UNKNOWN }} from {MODULE.as_uri()!r};
const d = GridDocument.fromSparse({{
  origin: [10, 20], shape: [3, 4], cellSize: 1,
  cells: [[1, 1], [2, 2]], blocked: [[0, 0]],
}});
d.beginTransaction();
d.setCell(1, 1, CELL_BLOCKED);
d.setCell(3, 4, CELL_FREE);
d.commitTransaction();
if (d.freeCount !== 2 || d.blockedCount !== 2) throw new Error("commit counts");
if (!d.undo() || d.getCell(1, 1) !== CELL_FREE || d.getCell(3, 4) !== CELL_UNKNOWN) {{
  throw new Error("undo state");
}}
if (!d.redo() || d.getCell(1, 1) !== CELL_BLOCKED || d.getCell(3, 4) !== CELL_FREE) {{
  throw new Error("redo state");
}}
d.beginTransaction();
d.setCell(3, 4, CELL_UNKNOWN);
d.commitTransaction();
if (d.bbox.x1 !== 2 || d.bbox.z1 !== 2) throw new Error("bbox shrink");
const sparse = d.exportSparse();
if (sparse.cells.length !== 1 || sparse.blocked.length !== 2) throw new Error("export counts");
d.rebase(1, 2);
if (d.getCell(1, 0) !== CELL_FREE || d.getCell(-1, -2) !== CELL_BLOCKED) {{
  throw new Error("rebase state");
}}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
