"""D3-20 专用真实 PostgreSQL 隔离测试入口，不访问原 55432 实例。

用法（仓库根目录、原项目 Python）：
  .venv/Scripts/python.exe -X utf8 scripts/round20_sandbox.py prepare
  .venv/Scripts/python.exe -X utf8 scripts/round20_sandbox.py status
  .venv/Scripts/python.exe -X utf8 scripts/round20_sandbox.py test -- -q tests/business/test_safety.py
  .venv/Scripts/python.exe -X utf8 scripts/round20_sandbox.py stop

仅复用原二进制、迁移和测试夹具；不下载、安装、调用模型或复制原业务数据。
prepare 仅为新隔离实例迁移/补齐 seed；test 中的原 conftest 重置隔离 test 库。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
from psycopg.conninfo import conninfo_to_dict

from services.business import manage
from services.business.db import connect
from services.business.scripts import local_postgres as pg

SANDBOX = ROOT / '.local' / 'd3-20' / 'sandbox'
DATA = SANDBOX / 'postgres-data'
RUNNER_MARKER = SANDBOX / 'round20-sandbox.json'
LOCK = SANDBOX / 'runner.lock'
PORT = 55433
BIN = ROOT / '.local' / 'postgres-17.11' / 'pgsql' / 'bin'
DEFAULT_TESTS = (
    'tests/business/test_safety.py', 'tests/business/test_corporate.py',
    'tests/business/test_reads.py', 'tests/business/test_observation.py',
)
RUNNER_ID = {
    'schema': 1, 'scope': 'D3-20-synthetic-only', 'root': str(ROOT),
    'data': str(DATA), 'bin': str(BIN), 'port': PORT,
    'workspace': 'test', 'fixtures': ['single-trip', 'two-trips-one-coupon'],
}


def checked_path(path: Path) -> Path:
    """拒绝 junction/symlink、工作区外路径以及原 PG 数据目录。"""
    if path.resolve() != path or not path.is_relative_to(ROOT):
        raise RuntimeError('拒绝重定向或工作区外路径。')
    if path == ROOT / '.local' / 'postgres-data':
        raise RuntimeError('拒绝原 PostgreSQL 数据目录。')
    return path


def bind_helper() -> None:
    # 只改变本 runner 进程内的模块常量，不修改原模块或原配置。
    for path in (SANDBOX, DATA, RUNNER_MARKER, LOCK, BIN):
        checked_path(path)
    pg.LOCAL = SANDBOX
    pg.DATA = DATA
    pg.MARKER = SANDBOX / 'postgres-project.json'
    pg.SECRET = SANDBOX / 'postgres-admin.json'
    pg.CONFIG = SANDBOX / 'business.json'
    pg.PORT = PORT
    pg.BIN = BIN
    for path in (pg.MARKER, pg.SECRET, pg.CONFIG):
        checked_path(path)


def check_runner(*, create: bool = False) -> None:
    checked_path(SANDBOX)
    if not RUNNER_MARKER.exists():
        if not create:
            raise RuntimeError('隔离实例尚未准备；先执行 prepare。')
        if SANDBOX.exists() and any(SANDBOX.iterdir()):
            raise RuntimeError('隔离目录已有未知内容，拒绝初始化。')
        SANDBOX.mkdir(parents=True, exist_ok=True)
        pg.save_new(RUNNER_MARKER, RUNNER_ID)
    if pg.load(RUNNER_MARKER) != RUNNER_ID:
        raise RuntimeError('D3-20 隔离实例标记不匹配。')


@contextmanager
def runner_lock():
    checked_path(LOCK)
    try:
        stream = LOCK.open('x', encoding='ascii')
    except FileExistsError:
        raise RuntimeError('隔离 runner 已占用或留有未核实锁；拒绝并发重置/启停。') from None
    try:
        with stream:
            stream.write(str(os.getpid()) + '\n')
        yield
    finally:
        checked_path(LOCK).unlink()


def check_cluster() -> dict:
    check_runner()
    marker = pg.validate()
    if not isinstance(marker.get('instance_id'), str) or not marker['instance_id']:
        raise RuntimeError('隔离 PostgreSQL 实例身份缺失。')
    for name, expected in (('port', str(PORT)), ('listen_addresses', '127.0.0.1')):
        actual = pg.command('postgres', '-D', str(DATA), '-C', name).stdout.strip()
        if actual != expected:
            raise RuntimeError('隔离 PostgreSQL 有效监听配置不匹配。')
    actual_data = pg.command('postgres', '-D', str(DATA), '-C', 'data_directory').stdout.strip()
    if Path(actual_data).resolve() != DATA:
        raise RuntimeError('隔离 PostgreSQL 有效数据目录不匹配。')
    return marker


def check_live() -> None:
    check_cluster()
    with pg.admin_connect() as connection:
        data, port, host = connection.execute(
            "SELECT current_setting('data_directory'), current_setting('port'), "
            "current_setting('listen_addresses')"
        ).fetchone()
        if Path(data).resolve() != DATA or port != str(PORT) or host != '127.0.0.1':
            raise RuntimeError('运行中的实例目录或端口不匹配。')


def checked_config() -> dict:
    """在建立业务连接前先验证凭证目标，绝不回退原 business.json。"""
    check_cluster()
    config = pg.load(pg.CONFIG)
    passwords = pg.load(pg.SECRET)
    if set(config) != {'database_url', 'test_database_url'}:
        raise RuntimeError('隔离业务配置字段不匹配。')
    for purpose, key in (('dev', 'database_url'), ('test', 'test_database_url')):
        parameters = conninfo_to_dict(config[key])
        expected = {
            'host': '127.0.0.1', 'port': str(PORT),
            'dbname': 'llm_business_' + purpose, 'user': 'llm_business_' + purpose,
            'password': passwords[purpose],
        }
        if parameters != expected:
            raise RuntimeError('隔离业务连接目标或凭证不匹配。')
    return config


def check_business(config: dict) -> dict:
    check_live()
    results = {}
    for purpose, key in (('demo', 'database_url'), ('test', 'test_database_url')):
        with connect(config[key]) as connection:
            if connection.execute("SELECT current_setting('port') AS port").fetchone()['port'] != str(PORT):
                raise RuntimeError('业务连接不在隔离端口。')
            if manage.database_purpose(connection) != purpose:
                raise RuntimeError('隔离数据库用途不匹配。')
            manage.check_marker(connection, purpose)
            rows = connection.execute('SELECT id,fixture_key FROM workspaces ORDER BY id').fetchall()
            expected_workspace = 'demo' if purpose == 'demo' else 'test'
            if len(rows) != 1 or rows[0]['id'] != expected_workspace:
                raise RuntimeError('隔离工作空间不匹配。')
            fixture = rows[0]['fixture_key']
            if fixture not in RUNNER_ID['fixtures'] or (purpose == 'demo' and fixture != 'single-trip'):
                raise RuntimeError('隔离工作空间夹具不匹配。')
            results[purpose] = {'workspace': expected_workspace, 'fixture': fixture}
    return results


def prepare() -> dict:
    for name in ('postgres', 'initdb', 'pg_ctl'):
        if not checked_path(BIN / (name + '.exe')).is_file():
            raise RuntimeError('缺少原项目 PostgreSQL 二进制；本入口不下载或安装。')
    if pg.command('postgres', '--version').stdout.strip() != 'postgres (PostgreSQL) ' + pg.VERSION:
        raise RuntimeError('原项目 PostgreSQL 二进制版本不匹配。')
    new = not DATA.exists()
    pg.initialize()
    if new:
        # 原 initialize 固定写入 55432；只在全新隔离配置末尾覆盖。
        with checked_path(DATA / 'postgresql.conf').open('a', encoding='utf-8') as stream:
            stream.write('\n# D3-20 synthetic-only isolated cluster\nport = 55433\n')
    check_cluster()
    pg.start()
    check_live()
    pg.provision()
    config = checked_config()
    # 新库没有表；已准备库只重放原迁移和补缺 seed，不 reset。
    for key, workspace in (('database_url', 'demo'), ('test_database_url', 'test')):
        manage.migrate(config[key])
        manage.seed(config[key], workspace)
    return check_business(config)


def report_status() -> None:
    check_cluster()
    active = pg.running()
    business = check_business(checked_config()) if active else None
    print(json.dumps({'scope': RUNNER_ID['scope'], 'port': PORT, 'running': active,
                      'data': str(DATA), 'business': business}, ensure_ascii=False, indent=2))


def redact(output: str, config: dict) -> str:
    secrets = list(config.values()) + list(pg.load(pg.SECRET).values())
    for value in sorted(secrets, key=len, reverse=True):
        output = output.replace(value, '[REDACTED]')
    return re.sub(r'postgres(?:ql)?://[^\s\'"<>]+', '[REDACTED_DATABASE_URL]', output)


def run_tests(arguments: list[str]) -> int:
    check_live()
    config = checked_config()
    check_business(config)
    if arguments and arguments[0] == '--':
        arguments = arguments[1:]
    if not arguments:
        arguments = ['-q', *DEFAULT_TESTS]
    child_env = os.environ.copy()
    # 仅子进程重定向；不改当前进程或用户/系统环境变量。
    child_env['BUSINESS_TEST_DATABASE_URL'] = config['test_database_url']
    child_env['BUSINESS_DATABASE_URL'] = config['database_url']
    child_env['BUSINESS_WORKSPACE'] = 'demo'
    child_env.pop('PYTEST_ADDOPTS', None)
    print('运行真实 PG 检查；原 conftest 仅重置 127.0.0.1:55433 的隔离 test 库。', flush=True)
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', '-m', 'pytest', *arguments], cwd=ROOT,
        env=child_env, capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    print(redact(result.stdout, config), end='')
    if result.stderr:
        print(redact(result.stderr, config), file=sys.stderr, end='')
    # 重读 marker/目录/端口及两种允许 fixture，不能以 pytest 退出码自证空间安全。
    check_business(config)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'status', 'stop', 'test'))
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('本入口仅复用 Windows 项目 PostgreSQL。')
    if args.action != 'test' and args.pytest_args:
        parser.error('仅 test 接受 pytest 参数。')
    bind_helper()
    check_runner(create=args.action == 'prepare')
    if args.action == 'status':
        report_status()
        return 0
    with runner_lock():
        if args.action == 'prepare':
            prepare()
            report_status()
        elif args.action == 'stop':
            check_cluster()
            if pg.running():
                check_live()
                pg.command('pg_ctl', '-D', str(DATA), '-m', 'fast', '-w', 'stop')
            print('D3-20 隔离 PostgreSQL 已停止，数据保留；原实例未操作。')
        else:
            return run_tests(args.pytest_args)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (psycopg.Error, ValueError, KeyError, OSError, RuntimeError) as error:
        # 连接异常、子进程/JSON异常可能携带秘密；仅输出错误类别。
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        else:
            print('隔离 runner 失败（' + type(error).__name__ + '）；检查隔离目录与实例状态。', file=sys.stderr)
        raise SystemExit(1) from None
