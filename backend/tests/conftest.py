import sys
from pathlib import Path

# 让 pytest 在 backend/ 或仓库根目录运行都能找到包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
