"""P38逐分句对象与本步核对生成遵循改进后的入口；只消费明确批准的Q29。"""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round29_task as task
if __name__ == '__main__':
    task.configure_round38()
    task.main()
