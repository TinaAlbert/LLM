"""P33 查询状态说明修复后的任务入口；只消费明确批准的Q24。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round29_task as task


if __name__ == '__main__':
    task.configure_round33()
    task.main()
