"""只在服务端加载连接配置，不把凭证放入响应或日志。"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WORKSPACE_FIXTURES = {"demo": "single-trip", "test": None,
                      "demo-coupon-large": "two-trips-one-coupon",
                      "demo-coupon-small": "two-trips-one-coupon"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    workspace_id: str = "demo"
    demo_login: bool = False
    mode: str = "local"

    def __post_init__(self):
        if self.demo_login and self.mode not in {"local", "test"}:
            raise ValueError("演示登录仅允许 local/test 模式")
        if not self.database_url or self.workspace_id not in WORKSPACE_FIXTURES:
            raise ValueError("需要项目数据库和明确工作空间")


def load_settings(*, test: bool = False) -> Settings:
    variable = "BUSINESS_TEST_DATABASE_URL" if test else "BUSINESS_DATABASE_URL"
    url = os.environ.get(variable)
    if not url:
        path = ROOT / ".local" / "business.json"
        if not path.exists():
            raise ValueError("请先按运行说明准备项目数据库配置")
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        url = config["test_database_url" if test else "database_url"]
    return Settings(
        database_url=url,
        workspace_id="test" if test else os.environ.get("BUSINESS_WORKSPACE", "demo"),
        demo_login=True if test else os.environ.get("BUSINESS_DEMO_LOGIN") == "1",
        mode="test" if test else os.environ.get("BUSINESS_MODE", "local"),
    )
