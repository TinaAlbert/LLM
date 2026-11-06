"""内部任务推进 CLI；凭证从忽略配置加载，控制面不提供给运行助手。"""

import argparse
import json

import psycopg

from .config import load_settings
from .worker import advance_time, expire, process_payment, process_ticket


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["payment", "ticket", "advance", "expire"])
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--id")
    parser.add_argument("--outcome", choices=["succeeded", "failed", "lost_response", "unknown"])
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args()
    try:
        settings = load_settings(test=args.test)
        url, workspace = settings.database_url, settings.workspace_id
        if args.action in {"payment", "ticket"}:
            if args.id is None or args.seconds is not None:
                raise ValueError("任务推进需要 --id，且不能携带 --seconds")
            process = process_payment if args.action == "payment" else process_ticket
            result = process(url, workspace, args.id, args.outcome or "succeeded")
        else:
            if args.id is not None or args.outcome is not None:
                raise ValueError("时间和到期清理不接受 --id/--outcome")
            if args.action == "advance":
                if args.seconds is None:
                    raise ValueError("推进时间需要 --seconds")
                result = advance_time(url, workspace, args.seconds)
            else:
                if args.seconds is not None:
                    raise ValueError("到期清理不接受 --seconds")
                result = expire(url, workspace)
        print(json.dumps(result, ensure_ascii=False, default=str))
    except psycopg.Error:
        # 连接异常原文可能包含凭证或主机信息，不能打印到 CLI/报告。
        parser.exit(1, "PostgreSQL 操作失败；检查项目配置、实例状态与迁移。\n")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
