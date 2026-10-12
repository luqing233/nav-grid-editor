# -*- coding: utf-8 -*-
"""``tests/data/floor_rule_vectors.json`` 的执行器（跨仓库契约测试）。

那份向量定义的是「三维坐标 → 玩家在哪一层」的判定语义。ok-end-field 会独立实现
同一份判定（它的跟随器不能依赖编辑器进程），两边各跑这组向量，任何一侧改了行为
另一侧就红。所以这里的断言不是"我的实现恰好这样"，而是"契约要求这样"。

向量文件里 ``contract: false`` 的用例是当前实现的偶然行为，允许改动；它们仍然跑，
但失败时不会被当成契约破坏 —— 那正是提醒"这里被改过了"的信号。
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from nav_grid_editor.services.maps import MapService

VECTORS = Path(__file__).resolve().parent / "data" / "floor_rule_vectors.json"


class FloorRuleVectorsTest(unittest.TestCase):
    def _service(self, tmp: Path, vector: dict) -> MapService:
        root = Path(tmp)
        (root / "t" / "layers").mkdir(parents=True, exist_ok=True)
        # 必须显式写一份目录（哪怕是空的）：不写的话 _layer_entries 会退回内置的
        # Map02Lv002 兜底目录，把用例污染掉。
        (root / "t" / "layers" / "catalog.json").write_text(
            json.dumps({"layers": vector.get("catalog") or []},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        svc = MapService(
            data_root=root,
            tiles_root=root / "t",
            profile_dir=root / "p",
            grid2d_dir=root / "g",
        )
        for grid in vector.get("grids") or []:
            cells = grid.get("cells") or [[0, 0]]
            data = {
                "origin": grid.get("origin") or [0, 0, 0],
                "cell_size": grid.get("cell_size", 1),
                "cells": cells,
                "blocked": grid.get("blocked") or [],
            }
            # floor_regions_raw：先按空区间正常存下，再绕过校验把非法区间写回 npz，
            # 模拟「文件里已经躺着一段坏区间」（历史数据 / 手改文件）。那段读出来才
            # 会被 _normalize_floor_regions 拒掉，正是要覆盖的路径。
            raw = grid.get("floor_regions_raw")
            data["floor_regions"] = [] if raw is not None else (
                grid.get("floor_regions") or [])
            result = svc.save_grid2d("map02", "4", data,
                                     tier=grid.get("tier", ""))
            self.assertTrue(result["ok"], result)
            if raw is not None:
                _inject_raw_regions(svc, grid.get("tier", ""), raw)
        return svc

    def test_vectors(self):
        payload = json.loads(VECTORS.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("vectors"), "向量文件不该是空的")
        soft_failures = []
        for vector in payload["vectors"]:
            with self.subTest(vector=vector["name"]):
                with tempfile.TemporaryDirectory() as tmp:
                    svc = self._service(Path(tmp), vector)
                    query = vector["query"]
                    got = svc.resolve_position_floor(
                        query["map"], query["level_id"],
                        _number(query["x"]), _number(query["y"]),
                        _number(query["z"]),
                    )
                expect = vector["expect"]
                same = (
                    got["status"] == expect["status"]
                    and got["tier_id"] == expect["tier_id"]
                    and sorted(got["matched"]) == sorted(expect["matched"])
                )
                if same:
                    continue
                message = (f"{vector['name']}: 期望 {expect}，实际 {got}"
                           + (f"（{vector['why']}）" if vector.get("why") else ""))
                if vector.get("contract", True):
                    self.fail(message)
                soft_failures.append(message)
        if soft_failures:
            # 非契约用例失配不算失败，但要显式报出来，免得被当成"通过"忽略掉
            print("\n[floor_rule] 非契约用例的行为已改变（允许，但需同步消费侧）:")
            for text in soft_failures:
                print("  - " + text)


def _number(value):
    if isinstance(value, str):
        return float(value)  # "nan" / "inf" 走这条路，正好测非有限数
    return value


def _inject_raw_regions(svc: MapService, tier: str, regions) -> None:
    """绕过校验，把非法区间直接写进 npz 的 meta。

    ``save_grid_floor_regions`` 会先校验，所以这里自己重写一次 npz，模拟"文件里
    已经躺着一段坏区间"（历史数据/手改文件）。
    """
    import numpy as np
    import json as _json

    path = svc.grid2d_path("map02", "4", tier)
    with np.load(path, allow_pickle=False) as z:
        arr = np.asarray(z["cells"])
        meta = _json.loads(np.asarray(z["meta"]).item())
    meta["floor_regions"] = regions
    with open(path, "wb") as fh:
        np.savez_compressed(
            fh, cells=arr,
            meta=np.array(_json.dumps(meta, ensure_ascii=False)),
        )


if __name__ == "__main__":
    unittest.main()
