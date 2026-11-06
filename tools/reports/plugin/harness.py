"""插件本地测试台架：不上传即可在开发机验证插件（连真实工厂库，只读）。

做什么：
  1. 读取 .env 获得工厂测试库连接（必须在开发包目录建 .env；仓库内开发可回退 backend/.env）
  2. 构造符合契约的 api 对象，执行插件的 generate() 并保存产物到 _out/<产品族>/
  3. 按线上 executor._validate_artifacts 的口径校验产物清单，打印产物与失败项

取数引擎统一走 engine/ 镜像（与线上 backend/app/services/reports/extractor.py
手动保持同步，取数行为一致）。

注意：台架与线上**不等价**，仅覆盖「取数 + 产物清单校验」。差异见
插件开发规则.md Step 4（template 走本地文件、standards 读 JSON、work_dir 不同、
log 仅 print、无 300s 超时与异常包装）。台架通过是必要不充分条件。

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
    """装载取数引擎与 .env：引擎统一走 engine/ 镜像；.env 只认本包目录。

    独立分发包脱离仓库后 parents[2]/backend/.env 不存在，若静默跳过，
    开发者只会看到一长串 sqlalchemy 连接栈；这里改为启动即给明确指引。
    """
    sys.path.insert(0, str(HERE / "engine"))
    env_file = HERE / ".env"
    if not env_file.exists():
        # 仅仓库内开发时兼容：独立分发包不会命中此路径
        fallback = HERE.parents[2] / "backend" / ".env" if len(HERE.parents) > 2 else None
        if fallback is not None and fallback.exists():
            env_file = fallback
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

    from app.services.reports import extractor

    if not extractor.configured():
        print(
            "未检测到工厂测试库配置：请在开发包目录复制 .env.example 为 .env，\n"
            "填写 FACTORY_DB_HOST / FACTORY_DB_PORT / FACTORY_DB_USER / FACTORY_DB_PASSWORD\n"
            "（只读账号），并确认内网 / VPN 可达。\n"
            f"当前查找路径：{HERE / '.env'}"
        )
        sys.exit(2)

class Api:
    """契约 api 的本地实现（签名与服务端 ScriptApi 一致；取数走只读连接）。"""

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

def _validate_artifacts(raw, work_dir: Path):
    """按线上 executor._validate_artifacts 的口径校验并规范化产物清单。

    覆盖：返回值必须是 list[dict]；path 必须落在 work_dir 内且文件真实存在；
    filename 以 path.name 为准；mes_field 缺省回退 type（engine.upload_mes_if_ready 行为）。
    台架不覆盖：归档、PDF 转换、MES 上传、任务终态。
    """
    if not isinstance(raw, list):
        raise ValueError(f"generate() must return a list, got {type(raw).__name__}")
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"artifacts[{i}] is not a dict")
        path = Path(item.get("path") or (work_dir / item.get("filename", "")))
        if not str(path).startswith(str(work_dir)) or not path.exists():
            raise ValueError(f"artifacts[{i}].path missing or outside work_dir: {path}")
        art_type = str(item.get("type") or f"report_{i}")
        out.append({
            "type": art_type,
            "filename": path.name,  # 线上会覆盖插件声明的 filename
            "path": str(path),
            "pdf": bool(item.get("pdf")),
            "mes": bool(item.get("mes")),
            "mes_field": item.get("mes_field") or art_type,
            "failed_items": list(item.get("failed_items") or []),
        })
    return out

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
    outputs = _validate_artifacts(module.generate(api), api.work_dir)

    for o in outputs:
        print(f"产物 {o['type']}: {o['filename']} (pdf={o['pdf']}, mes={o['mes']}, "
              f"mes_field={o['mes_field']}, 失败项 {len(o['failed_items'])})")
        if o["mes"] and not o["pdf"]:
            print("    ! mes=true 但 pdf=false：线上会 MES failed（missing pdf file）")
        for f in o["failed_items"][:5]:
            print(f"    - {f}")
    fields = [o["mes_field"] for o in outputs if o["mes"]]
    dup = sorted({f for f in fields if fields.count(f) > 1})
    if dup:
        print(f"警告：mes_field 重复 {dup} —— 线上 multipart 字段名会互相覆盖")
    print(f"结论：产物 {len(outputs)} 份，产物清单校验通过，文件均已落盘。")
    print(f"请打开 {api.work_dir} 与已知良好样张人工核对；上线前仍需在测试服走完整任务（见规则文档 Step 4 / Step 7）。")

if __name__ == "__main__":
    main()
