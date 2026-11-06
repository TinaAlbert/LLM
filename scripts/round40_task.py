"""P40导航结构工具与当前页生成遵循入口；只消费明确批准的Q31。"""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round29_task as task
if __name__ == '__main__':
    task.configure_round40()
    task.main()
