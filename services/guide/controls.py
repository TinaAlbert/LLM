"""跨线程/宿主进程的本项目有限槽位与分钟配额；饱和立即拒绝。"""
import json
import math
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / ".local"
_locks = [threading.Lock() for _ in range(16)]


class Busy(Exception):
    pass


class Slot:
    def __init__(self, index, root=ROOT):
        import msvcrt
        self.index, self.handle = index, None
        if not _locks[index].acquire(blocking=False):
            raise Busy("guide_busy")
        try:
            root.mkdir(parents=True, exist_ok=True)
            self.handle = open(root / "guide-resource-slots.lock", "a+b")
            if self.handle.seek(0, 2) < 16:
                self.handle.write(b"\0" * 16)
                self.handle.flush()
            self.handle.seek(index)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except BaseException:
            if self.handle:
                self.handle.close()
            self.handle = None
            _locks[index].release()
            raise Busy("guide_busy") from None

    def close(self):
        if self.handle is not None:
            import msvcrt
            self.handle.seek(self.index)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None
            _locks[self.index].release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def delegation_slot(root=ROOT):
    for index in range(8):
        try:
            return Slot(index, root)
        except Busy:
            continue
    raise Busy("active_delegation_limit")


def take_rate(kind, root=ROOT):
    limit = {"next": 6, "observation": 24}[kind]
    with Slot(10, root):
        path = root / "guide-resource-rates.json"
        now = time.time()
        try:
            def unique_pairs(items):
                data = {}
                for key, value in items:
                    if key in data:
                        raise ValueError("duplicate_field")
                    data[key] = value
                return data
            data = json.loads(path.read_text("utf-8"), object_pairs_hook=unique_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite"))) if path.exists() else {
                "schema": 1, "last_time": now, "next": [], "observation": []}
            if (not isinstance(data, dict) or set(data) != {"schema", "last_time", "next", "observation"}
                    or type(data["schema"]) is not int or data["schema"] != 1):
                raise ValueError()
            if (type(data["last_time"]) not in {int, float} or
                    not math.isfinite(data["last_time"]) or data["last_time"] < 0 or now < data["last_time"]):
                raise ValueError()
            for name in ("next", "observation"):
                if not isinstance(data[name], list) or len(data[name]) > {"next": 6, "observation": 24}[name]:
                    raise ValueError()
                if any(type(t) not in {int, float} or not math.isfinite(t) or
                       t < 0 or t > data["last_time"] for t in data[name]):
                    raise ValueError()
                data[name] = [t for t in data[name] if now - 60 < t <= now]
            if len(data[kind]) >= limit:
                raise Busy("guide_rate_limited")
            data[kind].append(now)
            data["last_time"] = now
            temporary = path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (ValueError, TypeError, KeyError, OSError):
            raise Busy("resource_accounting_unavailable") from None
