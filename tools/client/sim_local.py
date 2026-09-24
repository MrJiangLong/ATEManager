r"""本地测试库一键跑产线仿真（不改动 backend/.env）。

用法（多余参数原样透传给 line_simulator.py）：
    scripts\sim-local.bat
    scripts\sim-local.bat --units 20
    scripts\sim-local.bat --mode normal --clients 2 --units 20

    scripts\sim-local.bat --attach
    scripts\sim-local.bat --attach --units 20 --mode normal

默认模式（无 --attach）：自建一次性测试库 → seed → 起临时后端 → 仿真 → 收尾清理，
    与正式环境完全隔离。

--attach 模式（或 SIM_ATTACH=1）：不建库、不 seed、不起服务、不做任何清理，
    直接对 http://127.0.0.1:SIM_PORT 上**已在运行**的后端（本地 uvicorn 或
    Docker 部署均可）跑仿真；仿真数据写入该后端当前所连的数据库。

环境变量开关：
    SIM_PG=1            改用临时 Docker PG（postgres:15 @55433），锁语义等同正式
    SIM_DB=sqlite:///./data/sim.db  自定义 SQLite 库
    SIM_PRODUCTS=20     预置随机在制品数（0 可加速，静态规则仍写入）
    SIM_SWEEPER=false   关闭孤儿锁自动回收
    SIM_PORT=8000       后端端口
    SIM_KEEP_DB=1       复用上次测试库
    SIM_ATTACH=1        同 --attach

原理
    config.py 用 load_dotenv(override=False)，进程环境变量优先级最高，
    因此只需把 DATABASE_URL 放进子进程环境，即可临时把后端指向本地
    测试库，正式配置一个字都不用改。

为什么用 Python 而不是纯 bat
    cmd 解析 .bat 用 ANSI 代码页（936），中文注释与嵌套引号会被截断成
    乱码命令；编排逻辑放 Python 里可读、可维护，且不受代码页影响。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PY = BACKEND / ".venv" / "Scripts" / "python.exe"
STATE_DIR = BACKEND / ".sim_state"
ENV_FILE = BACKEND / ".env"

DEFAULT_SQLITE = "sqlite:///./data/sim.db"

def read_env_keys(*keys: str) -> dict:
    """从 backend/.env 解析指定键（跳过注释行，自动去掉首尾空白）。"""
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    out: dict = {}
    for key in keys:
        m = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(\S+)", text)
        if m:
            out[key] = m.group(1)
    return out

def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0

def wait_health(port: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False

def prepare_sqlite(sim_db: str, keep: bool) -> None:
    db_path = sim_db.split("///", 1)[-1]
    if not db_path or db_path.startswith(":memory:"):
        return
    target = (BACKEND / db_path).resolve()
    if not keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(target) + suffix)
            if p.exists():
                try:
                    p.unlink()
                except OSError as exc:
                    # 某些环境（IDE 安全删除策略）禁止 unlink；
                    print(f"[db][warn] 无法删除旧库 {p.name}（{exc}），改由 --reset 清空表")
        print(f"[db] rebuilding local SQLite test db {sim_db}")
    else:
        print(f"[db] reusing local SQLite test db {sim_db}")
    target.parent.mkdir(parents=True, exist_ok=True)

def prepare_pg() -> str:
    url = "postgresql+pg8000://postgres:postgres@127.0.0.1:55433/ate_sim"
    print("[db] starting throwaway Docker PG (ate-sim-pg, port 55433)...")
    subprocess.run(["docker", "rm", "-f", "ate-sim-pg"], capture_output=True)
    r = subprocess.run(
        ["docker", "run", "-d", "--name", "ate-sim-pg",
         "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=ate_sim",
         "-p", "55433:5432", "postgres:15"],
        capture_output=True,
    )
    if r.returncode != 0:
        print("[ERROR] failed to start Docker PG; unset SIM_PG to use SQLite")
        sys.exit(1)
    for _ in range(60):
        if subprocess.run(["docker", "exec", "ate-sim-pg", "pg_isready", "-U", "postgres", "-q"],
                          capture_output=True).returncode == 0:
            print(f"[db] PG ready: {url}")
            return url
        time.sleep(2)
    print("[ERROR] PG not ready")
    sys.exit(1)

def take_flag(argv: list, flag: str) -> tuple:
    """从参数里摘出开关（不传给 line_simulator），返回 (是否命中, 剩余参数)。"""
    return flag in argv, [a for a in argv if a != flag]

def run_simulator(port: int, cfg: dict, extra: list, env: dict) -> int:
    """对指定端口的后端跑产线仿真。"""
    cmd = [str(PY), str(ROOT / "tools" / "client" / "line_simulator.py"),
           "--base-url", f"http://127.0.0.1:{port}",
           "--api-key", cfg["V1_API_KEY"],
           "--admin-user", cfg.get("DEFAULT_ADMIN_USERNAME", "admin"),
           "--admin-password", cfg.get("DEFAULT_ADMIN_PASSWORD", "admin123")] + extra
    return subprocess.run(cmd, cwd=str(BACKEND), env=env).returncode

def main() -> int:
    attach_flag, extra = take_flag(sys.argv[1:], "--attach")
    attach = attach_flag or os.environ.get("SIM_ATTACH") == "1"
    port = int(os.environ.get("SIM_PORT", "8000"))
    products = os.environ.get("SIM_PRODUCTS", "20")
    sweeper = os.environ.get("SIM_SWEEPER", "false")
    use_pg = os.environ.get("SIM_PG") == "1"
    keep_db = os.environ.get("SIM_KEEP_DB") == "1"
    sim_db = os.environ.get("SIM_DB", DEFAULT_SQLITE)

    if not PY.exists():
        print("[ERROR] backend\\.venv not found. Run scripts\\setup.bat first.")
        return 1

    cfg = read_env_keys("V1_API_KEY", "DEFAULT_ADMIN_USERNAME", "DEFAULT_ADMIN_PASSWORD")
    if not cfg.get("V1_API_KEY"):
        print("[ERROR] cannot parse V1_API_KEY from backend\\.env")
        return 1

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    if attach:
        if not wait_health(port, timeout=10):
            print(f"[ERROR] no backend answering on http://127.0.0.1:{port}")
            print("        Start it first (scripts\\dev-backend.bat) or set SIM_PORT.")
            return 1
        print(f"[attach] target = running backend http://127.0.0.1:{port} "
              "(no db/seed/uvicorn, no cleanup)")
        print("[sim] starting line simulator...")
        rc = run_simulator(port, cfg, extra, env)
        print(f"[done] simulation finished, exit code {rc} (0=pass)")
        return rc

    # 端口必须空闲，否则仿真数据会写进正在运行的正式后端
    if port_open(port):
        print(f"[ERROR] port {port} already in use - a prod backend may be running.")
        print("        Stop it first, or set SIM_PORT=8100 to use another port.")
        return 1

    db_url = prepare_pg() if use_pg else (prepare_sqlite(sim_db, keep_db) or sim_db)

    env["DATABASE_URL"] = db_url
    env["SWEEPER_ENABLED"] = sweeper

    print("[seed] writing static process rules and scenario data...")
    if subprocess.run([str(PY), "-m", "app.seed", "--reset", "--products", products],
                      cwd=str(BACKEND), env=env).returncode != 0:
        print("[ERROR] seed failed")
        return 1

    print("[run] backend starting, waiting for health check...")
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND), env=env,
    )
    try:
        if not wait_health(port):
            print("[ERROR] backend not ready")
            return 1
        print(f"[run] backend ready: http://127.0.0.1:{port}")

        print("[sim] starting line simulator...")
        rc = run_simulator(port, cfg, extra, env)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        if use_pg:
            print("[db] removing throwaway PG container...")
            subprocess.run(["docker", "rm", "-f", "ate-sim-pg"], capture_output=True)

    print(f"[done] simulation finished, exit code {rc} (0=pass)")
    return rc

if __name__ == "__main__":
    sys.exit(main())

