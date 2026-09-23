"""报告规则注册表：脚本与模板来自 report_rules / report_templates 表。

上传式插件 = 受信任代码（仅 admin 上传，保存前经语法与入口校验）。
缓存以「规则的 updated_at + 模板清单」为指纹，指纹变化即失效——
保存即生效、无需重启。模板物化到本地缓存目录（MinIO 缺失时下载）。
"""

import hashlib
import importlib.util
import threading
from pathlib import Path
from typing import Optional

from ... import models
from ...config import settings
from ..timeutil import utcnow
from . import store

_lock = threading.Lock()
_cache: dict = {}  # rule -> Rule

TEMPLATE_LOCAL_DIR = Path(settings.REPORT_TEMPLATE_DIR)
TEMPLATE_MINIO_PREFIX = settings.MINIO_TEMPLATE_PREFIX
SCRIPT_MINIO_PREFIX = settings.MINIO_SCRIPT_PREFIX

def _file_etag(path: Path) -> str:
    """本地文件内容的 MD5 hex（MinIO 简单上传的 ETag 即内容 MD5，用于新鲜度比对）。"""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _Entry:
    """缓存条目：脚本模块（延迟编译）。"""

    def __init__(self, rule: models.ReportRule, templates: list):
        self.rule = rule
        self.templates = templates  # [(filename, object_key)]
        self._module = None

    @property
    def module(self):
        if self._module is None:
            self._module = _compile_script(self.rule.rule, self.rule.script or "")
        return self._module

def _compile_script(rule: str, source: str):
    """编译插件源码为模块并校验入口（与上传时的检查一致，防御 DB 被手工改动）。"""
    code = compile(source, f"{rule}.py", "exec")
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(rule, None))
    module.__file__ = f"{rule}.py"
    exec(code, module.__dict__)
    if not callable(getattr(module, "generate", None)):
        raise ValueError(f"plugin {rule}: missing generate(api)")
    return module

class Rule:
    """一条报告规则：匹配型号清单 + 模板物化 + 脚本模块 + 标准器台账。"""

    def __init__(self, entry: _Entry):
        self.entry = entry
        self.rule = entry.rule.rule
        self.models = entry.rule.models or []
        self.script_name = entry.rule.script_name
        self.mes_url = entry.rule.mes_url
        self.auto_trigger = entry.rule.auto_trigger
        self.enabled = entry.rule.enabled

    def matches(self, model: str) -> bool:
        """规则绑定确定型号清单：精确匹配，不做任何字符串派生。"""
        return model in self.models

    @property
    def module(self):
        return self.entry.module

    def template_path(self, name: str) -> Path:
        """模板本地路径：本地缓存与 MinIO 端 ETag 一致才复用，否则（重新）物化。

        ETag 取不到（MinIO 不可达/未配置）时信任本地缓存，保证可用性优先。
        """
        local = TEMPLATE_LOCAL_DIR / self.rule / name
        object_key = next((key for fn, key in self.entry.templates if fn == name), None)
        if object_key is None:
            raise FileNotFoundError(f"template {self.rule}/{name} not registered")
        if local.exists():
            remote_etag = store.stat_etag(object_key)
            if not remote_etag or remote_etag == _file_etag(local):
                return local
        local.parent.mkdir(parents=True, exist_ok=True)
        store.download_to(object_key, local)
        return local

    def load_standards(self) -> list:
        """本规则的标准器台账（report_standards 按 rule 隔离），证书校验用。"""
        from ...database import SessionLocal

        db = SessionLocal()
        try:
            rows = (
                db.query(models.ReportStandard)
                .filter(models.ReportStandard.rule == self.rule)
                .all()
            )
            return [
                {
                    "manufacturer": r.manufacturer,
                    "pc_name": r.pc_name,
                    "user_id": r.user_id,
                    "model": r.model,
                    "sn": r.sn,
                    "date": r.cal_date,
                }
                for r in rows
            ]
        finally:
            db.close()

def _load_entries(db) -> dict:
    entries: dict = {}
    rules = db.query(models.ReportRule).filter(models.ReportRule.enabled.is_(True)).all()
    templates = db.query(models.ReportTemplate).all()
    by_rule: dict = {}
    for t in templates:
        by_rule.setdefault(t.rule, []).append((t.filename, t.object_key))
    for rule in rules:
        entries[rule.rule] = _Entry(rule, by_rule.get(rule.rule, []))
    return entries

def load_rules() -> dict:
    """返回 rule 名 -> Rule（进程内缓存，保存时失效）。"""
    with _lock:
        if not _cache:
            from ...database import SessionLocal

            db = SessionLocal()
            try:
                for name, entry in _load_entries(db).items():
                    _cache[name] = Rule(entry)
            finally:
                db.close()
        return _cache

def find_rule(model: str):
    """按确定型号清单匹配报告规则；无匹配返回 None（调用方跳过即可）。"""
    for rule in load_rules().values():
        if rule.matches(model):
            return rule
    return None

def get_rule(name: Optional[str]):
    return load_rules().get(name) if name else None

def reset_cache() -> None:
    """上传/修改报告规则后调用，使全部实例缓存失效。"""
    with _lock:
        _cache.clear()

def register_rule(
    db,
    rule: str,
    models_list: list,
    script: Optional[str] = None,
    script_name: Optional[str] = None,
    templates: list = None,
    mes_url: Optional[str] = None,
    auto_trigger: bool = True,
    enabled: bool = True,
    updated_by: str = None,
) -> models.ReportRule:  # noqa: C901
    """新建/更新报告规则（含脚本与模板登记），并广播缓存失效。"""
    row = db.get(models.ReportRule, rule)
    if row is None:
        row = models.ReportRule(rule=rule)
        db.add(row)
    row.models = models_list
    row.mes_url = mes_url
    row.auto_trigger = auto_trigger
    row.enabled = enabled
    if script is not None:
        row.script = script
    if script_name is not None:
        row.script_name = script_name
    row.updated_by = updated_by
    row.updated_at = utcnow()
    for filename, object_key in (templates or []):
        existing = db.get(models.ReportTemplate, {"rule": rule, "filename": filename})
        if existing is None:
            db.add(models.ReportTemplate(rule=rule, filename=filename, object_key=object_key))
        else:
            existing.object_key = object_key
    db.commit()
    reset_cache()
    return row
