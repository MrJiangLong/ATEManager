"""出厂报告引擎：盖章完成 → 执行插件脚本（只读取数、产出 Excel）→ MinIO 归档 → PDF → MES。

产品细节上传式插件化：脚本（generate(api)，受信任代码）+ xlsx 模板均经 Web 由 admin
上传，存 report_rules / report_templates 表，保存即生效。引擎代码零改动即可扩展新报告规则。
"""

from . import registry

__all__ = ["registry"]
