"""P30新前瞻任务入口；复用29运输，只能消费另行明确批准的Q20。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import round29_task as task

def main():
    task.configure_round30()
    task.main()

if __name__ == '__main__':
    main()
