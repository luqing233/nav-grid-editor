"""nav-grid-editor 回归测试包。

未安装包时也能直接跑：把仓库的 ``src/`` 加进 ``sys.path``。
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
