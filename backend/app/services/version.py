"""版本基线匹配：固件基线与测试软件基线共用。

上位机 *IDN? 直读的固件版本形如 ``V3.20``；测试软件版本来自 config.toml 的
``[system].version``。直接按字符串比较会踩字典序的坑（``"V3.9" > "V3.10"``），
因此解析成数字元组再比较。

进站闸门（gate）、在制品视图（views.fw_match）与测试软件基线校验共用本模块，
避免两处口径漂移。
"""

import re
from typing import Optional, Tuple

from ..models import FW_RULE_MIN

Version = Tuple[int, ...]

_DIGITS = re.compile(r"\d+")


def parse_version(value: Optional[str]) -> Optional[Version]:
    """``V3.20`` / ``3.20.1`` → ``(3, 20, 1)``；含无法解析的段时返回 None。"""
    if not value:
        return None
    parts: list = []
    for segment in str(value).strip().split("."):
        matched = _DIGITS.search(segment)
        if matched is None:
            return None
        parts.append(int(matched.group()))
    return tuple(parts) if parts else None


def version_matches(actual: Optional[str], expected: Optional[str], rule: Optional[str]) -> bool:
    """版本基线比较：实际版本是否满足基线（固件基线 / 测试软件基线共用）。

    rule=exact  必须与基线完全一致（默认，向后兼容既有产线）
    rule=min    不低于基线即可
    """
    a = (actual or "").strip()
    e = (expected or "").strip()
    if not e:
        return True
    if rule != FW_RULE_MIN:
        return a == e

    actual_v, expected_v = parse_version(a), parse_version(e)
    if actual_v is not None and expected_v is not None:
        return actual_v >= expected_v
    # 版本里含日期/哈希等不可解析内容时退回字典序，至少保证行为确定
    return a >= e
