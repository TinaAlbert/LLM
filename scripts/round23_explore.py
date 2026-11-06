"""P23 独立报告/运行入口；复用22合成流程，默认完整隔离门保持失败关闭。"""
from __future__ import annotations

from pathlib import Path
import sys
import argparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import round22_explore as previous
from services.guide.exploration import ExplorationError

OUT = ROOT / ".local/d3-23/exploration"
REPORT = ROOT / ".local/d3-23/native-isolation.json"
SCOPE = "P23-synthetic-ordinary-role"
BINDING_PATH = "/__round23/binding"


def configure_server(*, marker_directory=OUT, scope=SCOPE):
    """返回(app, settings)；咨询可传自己的目录，报告仍与P23默认门一致。"""
    return previous.configure_server(marker_directory=marker_directory, scope=scope,
        binding_path=BINDING_PATH, report_path=REPORT, exclusive=True)


def server():
    previous.server(output_directory=OUT, scope=SCOPE, binding_path=BINDING_PATH,
                    report_path=REPORT, exclusive=True)


def run(*, server_marker_directory=OUT, server_scope=SCOPE):
    previous.run(output_directory=OUT, scope=server_scope, binding_path=BINDING_PATH, report_path=REPORT,
                 server_marker_directory=server_marker_directory)


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("action", choices=("server", "run"))
        parser.add_argument("--server-marker-directory", type=Path, default=OUT)
        parser.add_argument("--server-scope", default=SCOPE)
        args = parser.parse_args()
        if args.action == "server":
            if args.server_marker_directory != OUT or args.server_scope != SCOPE:
                parser.error("server使用固定P23探索目录与scope")
            server()
        else:
            run(server_marker_directory=args.server_marker_directory, server_scope=args.server_scope)
    except (RuntimeError, ExplorationError) as error:
        raise SystemExit(str(error)) from None
    except (previous.psycopg.Error, previous.httpx.HTTPError, ValueError, KeyError, OSError):
        raise SystemExit("P23工程入口失败；检查专用server/55433/23隔离证据，不输出凭证或完整响应。") from None
