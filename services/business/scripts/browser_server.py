"""仅供串行浏览器验收的本地测试库API启动入口；无控制HTTP。"""
import uvicorn
import psycopg
from services.business.app import create_app
from services.business.config import load_settings
from services.business.db import connect
from services.business.manage import check_marker, database_purpose


def main():
    settings = load_settings(test=True)
    with connect(settings.database_url) as conn:
        if database_purpose(conn) != 'test':
            raise SystemExit('浏览器验收拒绝连接非测试库')
        check_marker(conn, 'test')
    uvicorn.run(create_app(settings), host='127.0.0.1', port=8000, access_log=False, proxy_headers=False)


if __name__ == '__main__':
    try:
        main()
    except psycopg.Error:
        raise SystemExit('测试PG不可用；检查保留实例和忽略配置。') from None
