"""`.venv/bin/python -m console` : build (if needed) and serve the Equity Console on http://127.0.0.1:8787.

    --build   force `npm ci` + `npm run build`
    --dev     run `next dev` (port 3000, proxies /api) alongside the API, with auto-reload of the API
    --port N  API port (default 8787); binds to 127.0.0.1 only
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "console" / "web"
OUT = WEB / "out"
SRC_DIRS = ["app", "components", "lib"]
SRC_FILES = ["package.json", "next.config.ts", "tsconfig.json"]


def newest_source_mtime() -> float:
    m = 0.0
    for d in SRC_DIRS:
        for p in (WEB / d).rglob("*"):
            if p.is_file():
                m = max(m, p.stat().st_mtime)
    for f in SRC_FILES:
        if (WEB / f).exists():
            m = max(m, (WEB / f).stat().st_mtime)
    return m


def stale() -> bool:
    idx = OUT / "index.html"
    return not idx.exists() or idx.stat().st_mtime < newest_source_mtime()


def npm(*args: str) -> None:
    if not shutil.which("npm"):
        sys.exit("npm not found; install Node 20+ to build the web app")
    print("+ npm", *args, flush=True)
    subprocess.run(["npm", *args], cwd=WEB, check=True)


def build(force: bool = False) -> None:
    if not force and not stale():
        return
    lock = WEB / "package-lock.json"
    if not (WEB / "node_modules" / "next").exists() or force:
        npm("ci" if lock.exists() else "install", "--no-audit", "--no-fund")
    npm("run", "build")


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m console")
    ap.add_argument("--build", action="store_true", help="force a web rebuild")
    ap.add_argument("--dev", action="store_true", help="run next dev (port 3000) alongside the API")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    dev_proc = None
    if a.dev:
        if not (WEB / "node_modules" / "next").exists():
            npm("install", "--no-audit", "--no-fund")
        dev_proc = subprocess.Popen(["npm", "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "3000"], cwd=WEB)
        print("web dev server: http://127.0.0.1:3000  (proxies /api to :%d)" % a.port)
    else:
        build(a.build)
    import uvicorn
    try:
        print(f"Equity Console: http://127.0.0.1:{a.port}")
        if a.dev:
            uvicorn.run("console.api.app:app", host="127.0.0.1", port=a.port, reload=True, reload_dirs=[str(ROOT / "console" / "api"), str(ROOT / "library")])
        else:
            uvicorn.run("console.api.app:app", host="127.0.0.1", port=a.port, log_level="info")
    finally:
        if dev_proc:
            dev_proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
