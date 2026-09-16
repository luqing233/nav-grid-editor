"""终末地地图采集 / 标定 / 2D 网格编辑工具。

模块划分：

- :mod:`nav_grid_editor.api.app`                 HTTP 路由
- :mod:`nav_grid_editor.services.maps`           瓦片、标定与 2D 网格
- :mod:`nav_grid_editor.services.marks`          地图标记抓取
- :mod:`nav_grid_editor.integrations.smsdk`      设备 ID 与数美 SMSdk
- :mod:`nav_grid_editor.cli`                     命令入口
- :mod:`nav_grid_editor.web`                     前端页面与静态资源
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
