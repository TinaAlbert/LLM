"""Create/reuse only the repository .venv and install its verified lock."""

from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
# Isolated pip ignores PIP_CACHE_DIR; pass the chosen cache explicitly.
CACHE = Path(os.environ.get("PIP_CACHE_DIR") or str(ROOT / ".local" / "pip-cache")).resolve()


def main() -> None:
    required = (ROOT / ".python-version").read_text().strip()
    actual = ".".join(str(v) for v in sys.version_info[:3])
    if actual != required:
        raise SystemExit(f"Expected Python {required}; received {actual}.")
    if VENV.resolve() != VENV:
        raise SystemExit("Refusing a redirected project environment.")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PIP_CONFIG_FILE"] = os.devnull
    if not PYTHON.exists():
        if VENV.exists():
            raise SystemExit("Existing .venv is incomplete; inspect it manually before recovery.")
        subprocess.run([sys.executable, "-I", "-m", "venv", str(VENV)], check=True, env=env)
    config = (VENV / "pyvenv.cfg").read_text().lower()
    if "include-system-site-packages = false" not in config:
        raise SystemExit("Refusing an environment exposing system packages.")
    identity = subprocess.run(
        [str(PYTHON), "-I", "-c",
         "import json,sys; print(json.dumps([list(sys.version_info[:3]),sys.prefix]))"],
        capture_output=True, text=True, check=True, env=env,
    )
    version, prefix = json.loads(identity.stdout)
    if ".".join(map(str, version)) != required or Path(prefix).resolve() != VENV:
        raise SystemExit("Existing project interpreter version/prefix does not match the lock.")
    check = subprocess.run(
        [str(PYTHON), "-I", "-m", "pip", "--isolated", "config", "list"],
        capture_output=True, text=True, check=True, env=env,
    )
    # No configuration may redirect an installation outside this environment.
    if any(x in check.stdout.lower() for x in (".target=", ".prefix=", ".user=")):
        raise SystemExit("An active pip configuration redirects installation; refusing to install.")
    subprocess.run(
        [str(PYTHON), "-I", "-m", "pip", "--isolated", "--cache-dir", str(CACHE), "install",
         "--disable-pip-version-check", "--require-hashes", "--no-deps",
         "--index-url", "https://pypi.org/simple", "-r",
         str(ROOT / "services/business/requirements.lock")],
        check=True, env=env,
    )
    subprocess.run([str(PYTHON), "-I", "-m", "pip", "check"], check=True, env=env)
    audit = (
        "import importlib.metadata as m,pathlib,sys; "
        "p=pathlib.Path(sys.prefix).resolve(); "
        "assert all(pathlib.Path(d.locate_file('')).resolve().is_relative_to(p) "
        "for d in m.distributions()); print('All distributions are inside',p)"
    )
    subprocess.run([str(PYTHON), "-I", "-c", audit], check=True, env=env)


if __name__ == "__main__":
    main()
