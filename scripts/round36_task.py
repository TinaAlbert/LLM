"""P36当前动作修饰语/本步返回核对修复后的入口；只消费明确批准的Q27。"""
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import round29_task as task
if __name__ == '__main__':
    task.configure_round36()
    task.main()
