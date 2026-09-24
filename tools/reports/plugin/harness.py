"""插件本地测试台架：不上传即可在开发机验证插件（连真实工厂库，只读）。

做什么：
  1. 读取 .env 获得工厂测试库连接（本目录 .env；仓库内开发回退 backend/.env）
  2. 构造符合契约的 api 对象，执行插件的 generate() 并保存产物到 _out/<产品族>/
  3. 打印产物清单与失败项 —— 与线上执行路径（executor.ScriptApi）行为一致

取数引擎统一走 engine/ 镜像（与线上 backend/app/services/reports/extractor.py
手动保持同步，仓库内外行为一致，无运行形态差异）。

用法：
  python harness.py <插件目录或产品族名> <型号> <SN>
示例（在本目录下直接用 tek_mso）：
  python harness.py tek_mso DPO1074 CNUD001375
前提：插件目录下有 <family>.py 与 templates/；可选 standards.json（证书校验用）。
"""

import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLES = HERE

def _setup_runtime():
    """装载取数引擎与 .env：引擎统一走 engine/ 镜像；.env 优先本目录，仓库内回退 backend/.env。"""
    sys.path.insert(0, str(HERE / "engine"))
    env_file = HERE / ".env"
    if not env_file.exists():
        env_file = HERE.parents[2] / "backend" / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

class Api:
    """契约 api 的本地实现（与服务端注入行为一致；取数走只读连接）。"""

    def __init__(self, family_dir: Path, model: str, sn: str):
        self.family_dir = family_dir
        self.model, self.sn = model, sn
        # 产物统一落在插件根 _out/<family>/，保持 <family>/ 目录可整体打包发放
        self.work_dir = HERE / "_out" / family_dir.name / datetime.now().strftime("%H%M%S")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        std_file = family_dir / "standards.json"
        self.standards = json.loads(std_file.read_text(encoding="utf-8"))["standards"] if std_file.exists() else []

    def query(self, table, partition, select, filters):
        from app.services.reports import extractor

        return extractor.fetch_newest_pass(self.model, table, self.sn, select, partition, filters)

    def latest(self, table, column):
        from app.services.reports import extractor

        return extractor.fetch_latest(self.model, table, self.sn, column)

    def template(self, name):
        path = self.family_dir / "templates" / name
        if not path.exists():
            raise FileNotFoundError(f"模板缺失: {name}")
        return path

    def log(self, msg):
        print(f"  [plugin] {msg}")

def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    family_dir = Path(sys.argv[1]).resolve()
    if not family_dir.exists():  # 传产品族名 → 解析到本目录下的同名子目录
        family_dir = SAMPLES / sys.argv[1]
    model, sn = sys.argv[2], sys.argv[3]

    _setup_runtime()

    spec = importlib.util.spec_from_file_location(family_dir.name, family_dir / f"{family_dir.name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    api = Api(family_dir, model, sn)
    outputs = module.generate(api)  # 版本来源由脚本自定（api.latest）

    for o in outputs:
        print(f"产物 {o['type']}: {o['filename']} (pdf={o['pdf']}, mes={o['mes']}, 失败项 {len(o['failed_items'])})")
        for f in o["failed_items"][:5]:
            print(f"    - {f}")
    print(f"结论：产物 {len(outputs)} 份，文件均已落盘。请打开 {api.work_dir} 与已知良好样张人工核对。")

if __name__ == "__main__":
    main()
