# -*- coding: utf-8 -*-
"""包内路径与运行资源的回归测试。"""

import tomllib
import unittest
from pathlib import Path

from nav_grid_editor.integrations.smsdk import device_id
from nav_grid_editor.paths import CONFIG_DIR, PACKAGE_DIR, PROJECT_ROOT


class PackageLayoutTest(unittest.TestCase):
    def test_runtime_config_stays_at_repo_root(self):
        self.assertEqual(PROJECT_ROOT, PACKAGE_DIR.parents[1])
        self.assertEqual(CONFIG_DIR, PROJECT_ROOT / "configs")
        self.assertEqual(device_id.CONFIG_DIR, CONFIG_DIR)

    def test_web_and_smsdk_resources_exist(self):
        self.assertTrue((PACKAGE_DIR / "web" / "map_composer.html").is_file())
        self.assertTrue(
            (PACKAGE_DIR / "web" / "static" / "css" / "map_composer.css").is_file()
        )
        self.assertTrue(
            (PACKAGE_DIR / "web" / "static" / "js" / "map_composer.js").is_file()
        )
        self.assertTrue(
            (PACKAGE_DIR / "web" / "static" / "js" / "grid" / "grid-document.js").is_file()
        )
        self.assertTrue(
            (PACKAGE_DIR / "web" / "static" / "js" / "grid" / "grid-raster-cache.js").is_file()
        )
        self.assertTrue(
            (PACKAGE_DIR / "web" / "static" / "js" / "grid" / "grid-transport.js").is_file()
        )
        self.assertTrue(
            (PACKAGE_DIR / "web" / "static" / "js" / "render" / "render-scheduler.js").is_file()
        )
        self.assertTrue(device_id._RUNNER_PATH.is_file())

    def test_every_web_asset_is_covered_by_package_data(self):
        """web/ 下每个静态资源都要被 package-data 的 glob 覆盖。

        开发态是 StaticFiles 直接读目录，**漏配也不会报错**；只有装成 wheel
        之后才发现文件不在包里。所以这里反过来用 pyproject 的 pattern 去比对
        实际文件，让「新增一个 js 子目录」当场失败。
        """
        pyproject = PROJECT_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        patterns = data["tool"]["setuptools"]["package-data"]["nav_grid_editor"]

        covered = set()
        for pattern in patterns:
            if not pattern.startswith("web/"):
                continue
            covered.update(
                path.relative_to(PACKAGE_DIR).as_posix()
                for path in PACKAGE_DIR.glob(pattern)
            )

        web_root = PACKAGE_DIR / "web"
        actual = {
            path.relative_to(PACKAGE_DIR).as_posix()
            for path in web_root.rglob("*")
            if path.is_file() and path.suffix in (".html", ".css", ".js")
        }
        self.assertTrue(actual, "web/ 下应当有静态资源")
        self.assertEqual(sorted(actual - covered), [])


if __name__ == "__main__":
    unittest.main()
