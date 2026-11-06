"""P25 两份简短答复的零模型精确预览；仅 server/prepare，不创建许可。"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import round23_consult as base

OUT = ROOT / '.local/d3-25/consult-ready'
STOP = OUT / 'server.stop'
SCOPE = 'P25-consult-synthetic-ordinary-role'
QUESTION_INITIAL = (
    '我想为这两个行程都拿到票，并尽量少花钱。只答当前下一步、必要条件和核对点，'
    '100字内；区分预估与已执行，在当前页回答，不导航。'
)
QUESTION_CHANGED = (
    '我先锁定了第二个行程。券现在属于谁，还能调整顺序吗？只答当前下一步、必要条件和核对点，'
    '100字内；区分预估与已执行，在当前页回答，不导航。'
)
QUESTIONS = (QUESTION_INITIAL, QUESTION_CHANGED)


def bind():
    """仅绑定本进程；旧脚本和19–24产物保持原样。"""
    base.OUT, base.STOP, base.SCOPE = OUT, STOP, SCOPE
    base.QUESTION_INITIAL, base.QUESTION_CHANGED = QUESTIONS
    base.QUESTIONS = QUESTIONS


def server():
    bind()
    base.server()


def prepare():
    bind()
    base.prepare()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('server', 'prepare'))
    action = parser.parse_args().action
    try:
        prepare() if action == 'prepare' else server()
    except (KeyboardInterrupt, AssertionError, RuntimeError, ValueError, KeyError, OSError,
            base.psycopg.Error, base.httpx.HTTPError):
        raise SystemExit(
            'P25离线准备停止；保留已有证据，不自动重试或覆盖，不输出凭证。'
        ) from None
