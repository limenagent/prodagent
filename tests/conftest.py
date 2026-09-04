"""确保无论从哪里启动 pytest，都能 import 到工作区里的 src 包。"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
