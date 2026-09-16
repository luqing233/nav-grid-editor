# -*- coding: utf-8 -*-
"""主命令分发与内置抓取命令的回归测试。"""

import contextlib
import io
import tempfile
import unittest
from unittest import mock

from nav_grid_editor import cli
from nav_grid_editor.commands import fetch_marks


class CliDispatchTest(unittest.TestCase):
    def test_fetch_marks_subcommand_is_dispatched(self):
        with mock.patch.object(fetch_marks, "main", return_value=17) as command:
            result = cli.main(["fetch-marks", "--out", "tmp"])
        self.assertEqual(result, 17)
        command.assert_called_once_with(["--out", "tmp"])


class FetchMarksCommandTest(unittest.TestCase):
    def test_command_can_run_without_external_script(self):
        stats = {
            "roles": 1,
            "requests": 2,
            "items": 3,
            "points": 4,
            "saved_marks": 5,
            "duplicates": 0,
            "structures": {},
            "structure_points": 0,
            "files": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = io.StringIO()
            with (
                mock.patch.object(fetch_marks, "read_hg_content", return_value="credential"),
                mock.patch.object(fetch_marks, "fetch_auth", return_value=stats) as fetch,
                contextlib.redirect_stdout(output),
            ):
                result = fetch_marks.main(
                    ["--out", tmp, "--skip-validate", "--per-level"]
                )

        self.assertEqual(result, 0)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["per_level"], True)
        self.assertIn("Total Points     : 4", output.getvalue())


if __name__ == "__main__":
    unittest.main()
