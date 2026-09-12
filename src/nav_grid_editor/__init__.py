"""终末地地图采集 / 标定 / 2D 网格编辑工具。

模块划分：

- :mod:`nav_grid_editor.map_service`  数据层：瓦片采集/合成、标定、2D 网格读写
- :mod:`nav_grid_editor.server`       HTTP 层：全部路由
- :mod:`nav_grid_editor.cli`          唯一入口：``nav-grid-editor`` / ``python -m nav_grid_editor``
- :mod:`nav_grid_editor.web`          前端页面（``map_composer.html``）
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
