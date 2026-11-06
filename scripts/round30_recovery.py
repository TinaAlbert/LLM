"""P30-02价格对象修复后的前瞻入口；仅消费另行明确批准的Q21。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import round29_task as task


def main():
    task.configure_round30_recovery()
    task.main()


if __name__ == '__main__':
    main()
