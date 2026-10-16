# -*- coding: utf-8 -*-
"""所有自家 JS 的语法检查（ESM）。

为什么需要它：`map_composer.js` 不被任何测试 import（只有浏览器才加载它），所以一个
**语法错误**能让页面直接白屏，而全套 163 个用例照样全过 —— 实测踩到过：字符串里用了
ASCII 双引号，浏览器报 `Uncaught SyntaxError`，python 这边一片绿。

顺带记一条坑：`node --check <file>.js` 对这类错误**返回 0**（它按 CommonJS 过一遍就
放行），得走 `node --input-type=module --check` 从 stdin 读才真的按 ESM 解析。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_ROOT = ROOT / "src" / "nav_grid_editor" / "web" / "static" / "js"


def _our_js_files():
    return sorted(
        path for path in JS_ROOT.rglob("*.js")
        if "vendor" not in path.relative_to(JS_ROOT).parts
    )


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class JsSyntaxTest(unittest.TestCase):
    def test_every_own_module_parses_as_esm(self):
        files = _our_js_files()
        self.assertGreaterEqual(len(files), 6, "自家 JS 文件数不对，检查路径")
        for path in files:
            with self.subTest(module=path.relative_to(JS_ROOT).as_posix()):
                result = subprocess.run(
                    ["node", "--input-type=module", "--check"],
                    input=path.read_bytes(), capture_output=True, timeout=120,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"{path.name} 语法错误（浏览器会白屏）：\n"
                    + result.stderr.decode("utf-8", "replace")[:800])

    def test_the_check_actually_catches_a_broken_string(self):
        """自证：拿一个和当初那次完全同样的错误，确认上面那条检查真的会红。"""
        result = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=b'import x from "y";\nconst s = "a"\xe6\xa5\xbc"b";\n',
            capture_output=True, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"SyntaxError", result.stderr)

    def test_vendor_is_excluded(self):
        """第三方代码不归我们管，别让它的风格问题把测试搞红。"""
        names = [p.name for p in _our_js_files()]
        self.assertNotIn("three.module.js", names)
        self.assertNotIn("OrbitControls.js", names)


if __name__ == "__main__":
    unittest.main()
