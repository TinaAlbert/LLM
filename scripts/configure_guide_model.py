"""交互式本地Key配置；不调用网络，不输出Key，不修改执行策略。"""
import getpass
import json
import os
from pathlib import Path
import warnings


def main():
    path = Path(__file__).resolve().parents[1] / ".local/guide-model.json"
    if path.exists():
        print("已有配置文件已保留，请在本地编辑；不要把内容发到聊天。")
        return 1
    try:
        with warnings.catch_warnings():
            # 无可信控制台时拒绝；不允许getpass回退到可见输入。
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("DeepSeek API Key（隐藏输入）：")
        if not 16 <= len(key) <= 512 or any(char.isspace() for char in key):
            print("Key格式无效，未保存。")
            return 1
        value = {
            "package": "DS-1007", "service": "deepseek-official", "model": "deepseek-flash",
            "model_revision": "DeepSeek-V4.1-Flash", "account_ref": "existing-deepseek", "api_key": key,
            "account_review": {"checked_on": "", "existing_account": True, "balance_available": True,
                               "model_access": False, "evidence_ref": ""},
            "price_review": {"checked_on": "", "input_per_million": "2", "output_per_million": "8",
                             "cache_hit_per_million": "0.04",
                             "additional_fee_upper_cny": None, "evidence_ref": ""},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # 排他首次创建也防止隐藏输入期间其他进程创建文件后被覆盖。
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        print("已保存到忽略的 .local/guide-model.json；未发API请求。")
        print("模型访问、当日价格/额外费用、完整输入上界和真实隔离仍需核验。")
        return 0
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("没有可用的隐藏输入或输入取消，未提交Key。")
        return 1
    except OSError:
        print("本地保存失败或文件已存在；请保留现有文件并检查本地路径。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
