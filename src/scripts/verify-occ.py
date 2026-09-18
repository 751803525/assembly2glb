"""
OCC 环境可用性验证脚本

用途：
    检查当前 Python 环境里的 OCC 是否可用，且版本与期望值一致。

用法：
    python verify-occ.py <期望的OCC版本>
    例：python verify-occ.py 7.7.2

退出码：
    0 = OCC 可用且版本匹配
    1 = OCC 无法 import
    2 = OCC 可 import 但版本不匹配
"""

import sys


def main() -> int:
    try:
        import OCC
        import OCC.Core  # noqa: F401 —— 显式 import 确认核心模块存在
    except Exception:
        return 1

    expected = sys.argv[1] if len(sys.argv) > 1 else ""
    actual = OCC.VERSION
    return 0 if actual == expected else 2


if __name__ == "__main__":
    sys.exit(main())
