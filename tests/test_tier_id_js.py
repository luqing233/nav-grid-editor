# -*- coding: utf-8 -*-
"""tier 两种拼写的 JS 侧归一化。

Python 侧有 tests/test_tier_spelling.py 盯同一件事。这里除了单测三个纯函数，还有一条
**源码级守卫**：`map_composer.js` 里不许再出现内联的 `__main__` ↔ `""` 转换。

那条守卫才是关键 —— 本次的 bug 不是"函数写错了"，而是同一个转换散在 14 处内联三元
里、有一处漏了。函数写得再对，也拦不住下一个人再内联一次。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "src" / "nav_grid_editor" / "web" / "static" / "js"
MODULE = JS / "grid" / "tier-id.js"
COMPOSER = JS / "map_composer.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class TierIdJsTest(unittest.TestCase):
    def _run(self, body: str) -> str:
        script = f"""
import {{ MAIN_TIER_ID, toDiskTier, toApiTier, sameTier }}
  from {MODULE.as_uri()!r};
{body}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise AssertionError(f"node 失败:\n{result.stderr}")
        return result.stdout.strip()

    def test_helpers_round_trip(self):
        out = self._run("""
console.log(JSON.stringify({
  diskMain: toDiskTier(MAIN_TIER_ID),
  diskEmpty: toDiskTier(""),
  diskTier: toDiskTier("scene_x"),
  apiEmpty: toApiTier(""),
  apiMain: toApiTier(MAIN_TIER_ID),
  apiTier: toApiTier("scene_x"),
  sameMixed: sameTier("", MAIN_TIER_ID),
  sameBoth: sameTier(MAIN_TIER_ID, MAIN_TIER_ID),
  sameDiff: sameTier("", "scene_x"),
  idempotentDisk: toDiskTier(toDiskTier(MAIN_TIER_ID)),
  idempotentApi: toApiTier(toApiTier("")),
}));
""")
        import json
        self.assertEqual(json.loads(out), {
            "diskMain": "", "diskEmpty": "", "diskTier": "scene_x",
            "apiEmpty": "__main__", "apiMain": "__main__",
            "apiTier": "scene_x",
            "sameMixed": True, "sameBoth": True, "sameDiff": False,
            "idempotentDisk": "", "idempotentApi": "__main__",
        })

    def test_no_inline_conversion_survives_in_map_composer(self):
        """源码级守卫：转换必须走 tier-id.js。

        这两条模式就是本次散落的内联写法。留着它们，下一个人再抄一次就又会漏 ——
        服务端那边漏一次已经造出过 `map02____main___4.grid.npz`。
        """
        source = COMPOSER.read_text(encoding="utf-8")
        for pattern in ('=== MAIN_TIER_ID ? ""', '=== "__main__" ? ""',
                        '|| "__main__"'):
            with self.subTest(pattern=pattern):
                self.assertNotIn(
                    pattern, source,
                    f"map_composer.js 里还有内联的 tier 转换（{pattern}）；"
                    "请改用 grid/tier-id.js 的 toDiskTier / toApiTier")


if __name__ == "__main__":
    unittest.main()
