# -*- coding: utf-8 -*-
"""包内路径与运行资源的回归测试。"""

import unittest

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
        self.assertTrue(device_id._RUNNER_PATH.is_file())


if __name__ == "__main__":
    unittest.main()
