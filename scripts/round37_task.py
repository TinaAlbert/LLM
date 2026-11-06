"""P37当前锁价返回结果关系修复后的入口；只消费明确批准的Q28。"""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round29_task as task
if __name__ == '__main__':
    task.configure_round37()
    task.main()
