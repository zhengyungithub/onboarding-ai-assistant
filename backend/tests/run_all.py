"""一键运行后端回归测试

用法（在 backend 目录下执行）：

    ./venv/Scripts/python.exe tests/run_all.py               # 全量回归
    ./venv/Scripts/python.exe tests/run_all.py -k visibility # 只跑匹配用例
    ./venv/Scripts/python.exe tests/run_all.py -x            # 首个失败即停

说明：测试运行在独立测试库 test_regression.db 上，不会读写真实的 hr_ai.db，
也不需要启动后端服务、不访问网络（Qdrant / Embedding / LLM 均被规避）。
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def main() -> int:
    try:
        import pytest
    except ImportError:
        print("未安装 pytest，请先执行：")
        print("    ./venv/Scripts/python.exe -m pip install -r requirements-dev.txt")
        return 2

    args = ["-v", "--tb=short", str(Path(__file__).resolve().parent), *sys.argv[1:]]
    return pytest.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
