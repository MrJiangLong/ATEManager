"""Tek MSO/DPO HD 系列出厂报告插件（参考实现，可作为新产品的开发范本）。

报告逻辑 1:1 移植自 TekReport 桌面工具（Report.cs / ExportTestReportModel.cs），
已经过与 ATEManager 引擎输出的逐单元格一致性差分验证。

══════════════════════ 插件契约（服务端注入 api）══════════════════════
    api.model / api.sn                  型号与序列号（型号派生由脚本自行解析）
    api.query(table, partition, select, filters) -> list[dict]
                                        工厂测试库最新 PASS（ROW_NUMBER 取 date 最新，
                                        连接级强制只读）。select 必须包含
                                        比对要用到的列（分区/过滤/取值列）。
    api.latest(table, column) -> str    该 SN 在表中最新一条记录的某列（date/version/tester）
    api.template(name) -> Path          上传模板的本地缓存路径
    api.work_dir -> Path                产物输出目录
    api.standards -> list[dict]         标准器台账（Web 端「报告产品族」页维护）
    api.log(msg)                        写任务日志

版本来源由脚本自定：version = api.latest("bandwidth", "version")。
返回: [{"type", "filename", "path", "pdf", "mes", "mes_field", "failed_items"}, ...]
  type : data_report / cal_report / certificate（引擎据此串接 PDF 与 MES）
  pdf  : 是否需要转 PDF；mes : 是否随 MES 上传
  mes_field : mes=true 时该产物 PDF 在 MES multipart 中的字段名
═════════════════════════════════════════════════════════════════════
"""

import re
from datetime import date as _date
from datetime import datetime
from pathlib import Path

import openpyxl

# ---------------------------------------------------------------- 常量
VERT12 = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5]
VERT13 = VERT12 + [10]
FREQ_BY_BANDWIDTH = {
    "50": [1e7, 2e7, 5e7],
    "70": [1e7, 2e7, 5e7, 7e7],
    "100": [1e7, 2e7, 5e7, 1e8],
    "default": [1e7, 2e7, 1e8, 1.5e8, 2e8],
}
DC_OFFSET_PAIRS = [(0.05, 2), (0.05, -2), (0.5, 25), (0.5, -25), (5, 60), (5, -60)]
BASE_NOISE_TB = [5e-8, 0.001, 0.01]
IMPEDANCE_V = [0.05, 0.5, 5]
AFGDC_STEPS = [-2.5, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 2.5]
PCB_VERSION = {"chan2": "PCB Version:V1.01.0000", "chan4": "PCB Version:V1.02.0000"}
EQUIPMENT_AFG = ", UNI-T UT8806 6.5 digits desktop digital multimeter"
REPORT_CODE = {"data_report": "P", "cal_report": "D", "certificate": "C"}

# ---------------------------------------------------------------- 值格式（Tek 口径）
def _fmt_v(v):
    return f"{v * 1000:.3f}mV" if round(v * 1000, 3) < 1000 else f"{v:.3f}V"

def _fmt_v_neg(v):
    return f"{v * 1000:.3f}mV" if round(abs(v) * 1000, 3) < 1000 else f"{v:.3f}V"

def _cs(x):
    """C# double.ToString() 口径：整数值不带 .0 尾巴（如 1 而非 1.0）。"""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)

def _cal_amp(v):
    return round(v * 1000, 2) if round(v * 1000, 3) < 1000 else round(v, 3)

def _cal_bw(v, vertical):
    return round(v * 1000, 3) if float(vertical) < 0.5 else round(v, 3)

def _num(v):
    return float(v)

# ---------------------------------------------------------------- 查找
def _find(rows, **conds):
    for r in rows:
        ok = True
        for k, v in conds.items():
            a = r.get(k)
            try:
                if abs(float(a) - float(v)) > 1e-9:
                    ok = False
                    break
            except (TypeError, ValueError):
                if str(a).strip() != str(v).strip():
                    ok = False
                    break
        if ok:
            return r
    return None

def _need(rows, **conds):
    r = _find(rows, **conds)
    if r is None:
        raise KeyError(str(conds))
    return r

def set_cell(sheet, row, col, value):
    """row/col 为 0 基（与 NPOI GetRow/GetCell 同基准）。"""
    if value is None:
        return
    sheet.cell(row=row + 1, column=col + 1, value=value)

def _replace_ph(sheet, row, col, token, value):
    cell = sheet.cell(row=row + 1, column=col + 1)
    text = str(cell.value or "")
    if token in text:
        cell.value = text.replace(token, str(value))

# ---------------------------------------------------------------- 数据报告
def _render_data(api, sheet, D, derived, test_date, version):
    """WriteDataReportTestResult 的 1:1 移植。缺数据：记 failed_items 并跳过该格，
    不中断整节（TekReport 会中止整节，判定结果一致，我们多填有效数据）。"""
    model = api.model
    chan, bw, has_afg = derived["chan_count"], derived["bandwidth"], derived["has_afg"]
    failed = []
    freqs = FREQ_BY_BANDWIDTH.get(str(bw), FREQ_BY_BANDWIDTH["default"])
    start2 = {2: 39, 4: 61}[chan]
    equipment = EQUIPMENT_AFG if has_afg else ""

    # 表头（WriteDataReportSheetHead）
    sheet["G2"] = f"Software Version:{version}"
    sheet["G3"] = f"Firmware Version:{version}"
    sheet["G4"] = PCB_VERSION["chan2" if chan == 2 else "chan4"]
    sheet["H6"] = test_date.strftime("%d/%m/%Y")
    _replace_ph(sheet, 4, 1, "<afgEqu>", equipment)

    def ph(row, col):
        _replace_ph(sheet, row, col, "<model>", model)

    # ---- 带宽·单通道（mode=1）：行=13+ch×11+freq×2，列=4+v ----
    try:
        start, er, ec = 13, 13, 4
        ph(13, 0)
        for ch in range(1, chan + 1):
            for v in VERT12:
                for f in freqs:
                    tr = _need(D["bw1"], chnum=ch, vertical=v, freq=f)
                    set_cell(sheet, er, ec, _fmt_v(_num(tr["value"])))
                    set_cell(sheet, er + 1, ec, _cs(round(_num(tr["gain"]), 4)))
                    er += 2
                er, ec = start + (ch - 1) * 11, ec + 1
            er, ec = start + ch * 11, 4
    except KeyError as e:
        failed.append(f"bw_1ch 缺数据 {e}")

    # ---- 带宽·四通道（mode=4）：行=39(2ch)/61(4ch)+ch×11+freq×2 ----
    try:
        start, er, ec = start2, start2, 4
        ph(start, 0)
        for ch in range(1, chan + 1):
            for v in VERT12:
                for f in freqs:
                    tr = _need(D["bw4"], chnum=ch, vertical=v, freq=f)
                    set_cell(sheet, er, ec, _fmt_v(_num(tr["value"])))
                    set_cell(sheet, er + 1, ec, _cs(round(_num(tr["gain"]), 4)))
                    er += 2
                er, ec = start + (ch - 1) * 11, ec + 1
            er, ec = start + ch * 11, 4
    except KeyError as e:
        failed.append(f"bw_4ch 缺数据 {e}")

    # ---- DC 增益：正/负 Vin，差分=pos−neg−2vin，误差=÷2vin ----
    try:
        start = 68 if chan == 2 else 112
        er, ec = start, 4
        ph(start, 0)
        for ch in range(1, chan + 1):
            for v in VERT13:
                tr = _need(D["dc_pos"], chnum=ch, vertical=v)
                value = _num(tr["value"])
                set_cell(sheet, er, ec, _fmt_v(value))
                v_neg = _num(_need(D["dc_neg"], chnum=ch, vertical=v)["value"])
                set_cell(sheet, er + 1, ec, _fmt_v_neg(v_neg))
                vin = _num(tr["vin"]) * 2
                dcdiff = value - v_neg - vin
                set_cell(sheet, er + 2, ec, _fmt_v_neg(dcdiff))
                set_cell(sheet, er + 3, ec, _cs(round(dcdiff / vin * 100, 2)) + "%")
                ec += 1
            er, ec = start + ch * 5, 4
    except KeyError as e:
        failed.append(f"dc 缺数据 {e}")

    # ---- 时基精度：×1e12 → ps ----
    try:
        row = 90 if chan == 2 else 146
        ph(row, 0)
        set_cell(sheet, row, 3, _cs(round(_num(D["timebase"][0]["value"]) * 1e12, 1)))
    except (KeyError, IndexError) as e:
        failed.append(f"timebase 缺数据 {e}")

    # ---- AFG 直流偏置（无 AFG 库中无数据则静默跳过；有 AFG 缺数据记失败 —— 与 TekReport 同口径）----
    try:
        er, ec = (98 if chan == 2 else 154), 1
        for o in AFGDC_STEPS:
            set_cell(sheet, er, ec, _cs(round(_num(_need(D["afgdc"], offsetvamp=o)["value"]), 4)))
            ec += 1
    except KeyError as e:
        if has_afg:
            failed.append(f"afgDc 缺数据 {e}")

    # ---- 输入阻抗：res MΩ@col、cap pF@col+2 ----
    try:
        start = 106 if chan == 2 else 162
        er, ec = start, 3
        ph(start, 0)
        for ch in range(1, chan + 1):
            for v in IMPEDANCE_V:
                tr = _need(D["impedance"], chnum=ch, vertical=v)
                set_cell(sheet, er, ec, round(_num(tr["resvalue"]) / 1e6, 4))
                set_cell(sheet, er, ec + 2, round(_num(tr["capvalue"]) * 1e12, 4))
                ec += 4
            er, ec = start + ch, 3
    except KeyError as e:
        failed.append(f"impedance 缺数据 {e}")

    # ---- DC 偏置：垂直档位×偏置幅值成对 ----
    try:
        start = 116 if chan == 2 else 174
        er, ec = start, 3
        ph(start, 0)
        for ch in range(1, chan + 1):
            for i, (v, o) in enumerate(DC_OFFSET_PAIRS):
                tr = _need(D["dcoffset"], chnum=ch, vertical=v, offsetvamp=o)
                set_cell(sheet, er, ec + i, _fmt_v(_num(tr["value"])))
            er, ec = start + ch * 2, 3
    except KeyError as e:
        failed.append(f"dc_offset 缺数据 {e}")

    # ---- 底噪：×1e6 → µV ----
    try:
        start = 126 if chan == 2 else 188
        er, ec = start, 3
        ph(start, 0)
        for ch in range(1, chan + 1):
            for v in [0.001, 0.002, 0.1]:
                for tb in BASE_NOISE_TB:
                    tr = _need(D["basenoise"], chnum=ch, vertical=v, timebase=tb)
                    set_cell(sheet, er, ec, round(_num(tr["value"]) * 1e6, 3))
                    er += 1
                er, ec = start + (ch - 1) * 5, ec + 2
            er, ec = start + ch * 5, 3
    except KeyError as e:
        failed.append(f"base_noise 缺数据 {e}")

    # ---- 相位延迟：CH12/CH34 ----
    try:
        row = 143 if chan == 2 else 215
        ph(row - 1, 0)
        set_cell(sheet, row, 3, round(_num(_need(D["phasedelay"], valuedscr="CH12")["value"]) * 1e12, 2))
        set_cell(sheet, row, 5, round(_num(_need(D["phasedelay"], valuedscr="CH34")["value"]) * 1e12, 2))
    except KeyError as e:
        failed.append(f"phase_delay 缺数据 {e}")

    # ---- AFG 正弦/方波（库中无表或无数据 → 跳过且不替换占位符；有 AFG 缺数据记失败 —— 同 TekReport）----
    try:
        row = 152 if chan == 2 else 224
        _need(D["afgsin"], freq=10000)
        _need(D["afgsin"], freq=25e6)
        ph(row - 1, 0)
        set_cell(sheet, row, 3, _num(_need(D["afgsin"], freq=10000)["value"]))
        set_cell(sheet, row, 4, _num(_need(D["afgsin"], freq=25e6)["value"]))
    except KeyError as e:
        if has_afg:
            failed.append(f"afgSine 缺数据 {e}")
    try:
        row = 161 if chan == 2 else 233
        _need(D["afgsquare"], freq=25000)
        _need(D["afgsquare"], freq=1e7)
        ph(row - 1, 0)
        set_cell(sheet, row, 3, _num(_need(D["afgsquare"], freq=25000)["value"]))
        set_cell(sheet, row, 4, _num(_need(D["afgsquare"], freq=1e7)["value"]))
    except KeyError as e:
        if has_afg:
            failed.append(f"afgSquare 缺数据 {e}")

    # ---- AuxOut / 本地信号 ----
    try:
        tr = D["auxout"][0]
        row = 175 if chan == 2 else 247
        ph(row, 0)
        ph(row, 6)
        set_cell(sheet, row, 3, _num(tr["amphmax"]))
        set_cell(sheet, row, 4, _num(tr["amphmin"]))
        set_cell(sheet, row, 9, _num(tr["amplmax"]))
        set_cell(sheet, row, 10, _num(tr["amplmin"]))
    except (KeyError, IndexError) as e:
        failed.append(f"auxout 缺数据 {e}")
    try:
        tr = D["localsignal"][0]
        row = 182 if chan == 2 else 254
        ph(row, 0)
        ph(row + 7, 0)
        set_cell(sheet, row, 3, _num(tr["ampvalue"]))
        set_cell(sheet, row, 4, _num(tr["freqvalue"]))
    except (KeyError, IndexError) as e:
        failed.append(f"local_signal 缺数据 {e}")
    return failed

# ---------------------------------------------------------------- 校准报告
def _render_cal(api, sheet, D, derived, test_date=None, version=None):
    """WriteCalibrationReportSheetHead（表头回填）+ 4 个数据节。"""
    chan = derived["chan_count"]
    freqs = FREQ_BY_BANDWIDTH.get(str(derived["bandwidth"]), FREQ_BY_BANDWIDTH["default"])
    failed = []
    # 表头（F13/F16/J16/N16/F77/D144/F204）
    cer_no = str(derived["cer_no"])
    for ref in ("F13", "F77", "D144", "F204"):
        sheet[ref] = cer_no
    sheet["F16"] = api.model
    sheet["J16"] = api.sn
    sheet["N16"] = (test_date or datetime.now()).strftime("%d/%m/%Y")
    # ---- 校准幅度（AC 耦合 13 档；通道块 13 行 + 1 空行分隔）----
    try:
        er, ec = 82, 9
        for ch in range(1, chan + 1):
            for v in VERT13:
                value = _num(_need(D["amplitude"], chnum=ch, vertical=v)["value"])
                set_cell(sheet, er, ec, _cal_amp(value))
                er += 1
            er += 1
    except KeyError as e:
        failed.append(f"cal_amp 缺数据 {e}")
    # ---- 校准带宽（单通道模式；列步进 2）----
    try:
        er, ec = 149, 7
        for ch in range(1, chan + 1):
            for v in VERT12:
                for f in freqs:
                    value = _num(_need(D["bw1"], chnum=ch, vertical=v, freq=f)["value"])
                    set_cell(sheet, er, ec, _cal_bw(value, v))
                    ec += 2
                er, ec = er + 1, 7
            er += 1
    except KeyError as e:
        failed.append(f"cal_bw 缺数据 {e}")
    # ---- 校准 AFG DC / 无 AFG 的 N/A 占位列 ----
    if derived["has_afg"]:
        try:
            er, ec = 209, 7
            for o in AFGDC_STEPS:
                set_cell(sheet, er, ec, round(_num(_need(D["afgdc"], offsetvamp=o)["value"]), 4))
                er += 1
        except KeyError as e:
            failed.append(f"cal_afgdc 缺数据 {e}")
    else:
        er = 209
        for _ in range(11):
            set_cell(sheet, er, 13, "N/A")
            er += 1
    # ---- 校准时基精度：×1e9 → ns ----
    try:
        set_cell(sheet, 227, 9, round(_num(D["timebase"][0]["value"]) * 1e9, 3))
    except (KeyError, IndexError) as e:
        failed.append(f"cal_timebase 缺数据 {e}")
    return failed

# ---------------------------------------------------------------- 证书
def _render_cert(api, sheet, D, derived, test_date, version):
    """WriteCertificateReportTestResult 的 1:1 移植（含标准器校验）。"""
    failed = []
    sheet["E10"] = str(derived["cer_no"])
    sheet["U11"] = api.model
    sheet["AD11"] = api.sn
    sheet["E12"] = f"{derived['bandwidth']}MHz Digital Phosphor Oscilloscope"
    sheet["E13"] = test_date.strftime("%d/%m/%Y")
    sheet["E14"] = version
    if not derived["has_afg"]:
        sheet["C33"] = ""
    if not api.standards:
        failed.append("Certificate: 标准器台账未配置")
        return failed

    def check(table, manufacturer, model_prefix, row):
        tester = api.latest(table, "tester").split("_")[0]
        info = _find_standard(api.standards, manufacturer, tester, model_prefix)
        if info is None:
            failed.append(f"Certificate: 台账中未找到 {manufacturer} {model_prefix}*（工位 {tester}）")
            return False
        std_date = _std_date(info)
        if std_date is not None and std_date < test_date.date():
            failed.append(f"Certificate: 标准器 {info.get('model')} 校准日期({std_date:%d/%m/%Y})早于被测件，需重新校准")
            return False
        sheet[f"C{row}"] = f"{str(info.get('manufacturer', '')).upper()}/{info.get('model')}"
        sheet[f"T{row}"] = info.get("sn")
        sheet[f"AD{row}"] = std_date.strftime("%d/%m/%Y") if std_date else ""
        return True

    ok = True
    for table, mfr, prefix, row in (
        ("bandwidth", "Fluke", "9500", 36),
        ("phasedelay", "UNI-T", "UTG", 37),
        ("auxout", "UNI-T", "MSO", 38),
    ):
        if not check(table, mfr, prefix, row):
            ok = False
    if derived["has_afg"]:
        if not check("afgdc", "UNI-T", "UT8", 39):
            ok = False
    else:
        for ref in ("C39", "K39", "T39", "AD39"):
            sheet[ref] = ""
    first = api.standards[0]
    sheet["AB70"] = first.get("user_id")
    sheet["AB72"] = test_date.strftime("%d/%m/%Y")
    return failed

def _find_standard(standards, manufacturer, pc_name, model_prefix):
    for info in standards:
        if (str(info.get("manufacturer", "")) == manufacturer
                and str(info.get("pc_name", "")) == pc_name
                and str(info.get("model", "")).startswith(model_prefix)):
            return info
    return None

def _std_date(info):
    raw = info.get("date")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, _date):
        return raw
    if isinstance(raw, str) and raw:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    return None

# ---------------------------------------------------------------- 入口
def _parse_date(raw):
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:26] if "." in raw else raw, fmt)
        except (ValueError, TypeError):
            continue
    return datetime.now()

def _derive(model: str, sn: str) -> dict:
    """型号/SN 字符串派生（产品族私有规则：末位数字/位切片/尾缀/SN 尾数）。"""
    d = re.search(r"(\d)", model[::-1])
    s = re.search(r"(\d+)$", sn)
    return {
        "chan_count": int(d.group(1)) if d else 0,
        "bandwidth": int(re.sub(r"\D", "", model[4:6]) or 0) * 10,
        "has_afg": model.endswith("S"),
        "cer_no": int(s.group(1)) if s else 0,
    }

_VERSION_TABLES = (
    "bandwidth", "dc", "timebaseaccuracy", "afgdc", "impedance", "dcoffset",
    "basenoise", "phasedelay", "afgsin", "afgsquare", "auxout", "localsignal", "amplitude",
)

def _latest_version(api) -> str:
    """跨全部测试表取 date 最新的 version（对齐 TekReport SQLHelper 的 UNION ALL 口径）。

    同日期平票时后遍历的表优先（同 C# table_idx DESC）；
    表不存在（如无 AFG 机型的 afg* 表）或无记录时跳过。
    """
    best_date, best_ver = "", ""
    for t in _VERSION_TABLES:
        try:
            d, v = api.latest(t, "date"), api.latest(t, "version")
        except Exception:
            continue
        if d and v and str(d) >= best_date:
            best_date, best_ver = str(d), str(v)
    return best_ver

def _tpl(base, derived):
    bw = str(derived["bandwidth"])
    suffix = {"50": "_50M", "70": "_70M", "100": "_100M"}.get(bw, "")
    return f"{base}_{derived['chan_count']}chan{suffix}.xlsx"

def generate(api):
    """插件入口：取数 → 渲染三份报告 → 返回产物清单（引擎接手 MinIO/PDF/MES）。"""
    model, sn = api.model, api.sn
    version = _latest_version(api)  # 版本来源由脚本自定（跨全部测试表取最新）
    derived = _derive(model, sn)
    api.log(f"派生属性: {derived}")

    D = {
        "bw1": api.query("bandwidth", ["sn", "vertical", "vin", "chnum", "freq"],
                         ["chnum", "vertical", "freq", "value", "gain"],
                         {"multichannelmode": 1, "result": "PASS"}),
        "bw4": api.query("bandwidth", ["sn", "vertical", "vin", "chnum", "freq"],
                         ["chnum", "vertical", "freq", "value", "gain"],
                         {"multichannelmode": 4, "result": "PASS"}),
        "dc": api.query("dc", ["sn", "vertical", "vin", "chnum"], ["chnum", "vertical", "vin", "value"],
                        {"result": "PASS"}),
        "timebase": api.query("timebaseaccuracy", ["sn"], ["value"], {"result": "PASS"}),
        "afgdc": api.query("afgdc", ["sn", "offsetvamp"], ["offsetvamp", "value"], {"result": "PASS"}),
        "impedance": api.query("impedance", ["sn", "vertical", "chnum"], ["chnum", "vertical", "resvalue", "capvalue"],
                               {"resresult": "PASS", "capresult": "PASS"}),
        "dcoffset": api.query("dcoffset", ["sn", "vertical", "offsetvamp", "chnum"],
                              ["chnum", "vertical", "offsetvamp", "value"], {"result": "PASS"}),
        "basenoise": api.query("basenoise", ["sn", "vertical", "chnum", "timebase"],
                               ["chnum", "vertical", "timebase", "value"], {"result": "PASS"}),
        "phasedelay": api.query("phasedelay", ["sn", "valuedscr"], ["valuedscr", "value"], {"result": "PASS"}),
        "afgsin": api.query("afgsin", ["sn", "freq"], ["freq", "value"], {"result": "PASS"}),
        "afgsquare": api.query("afgsquare", ["sn", "freq"], ["freq", "value"], {"result": "PASS"}),
        "auxout": api.query("auxout", ["sn"], ["amphmax", "amphmin", "amplmax", "amplmin"], {"result": "PASS"}),
        "localsignal": api.query("localsignal", ["sn"], ["ampvalue", "freqvalue"],
                                 {"ampresult": "PASS", "freqresult": "PASS"}),
        "amplitude": api.query("amplitude", ["sn", "couple", "vertical", "vin", "chnum"],
                               ["chnum", "vertical", "value"], {"couple": "AC", "result": "PASS"}),
    }
    dc_rows = D.pop("dc")
    D["dc_pos"] = [r for r in dc_rows if _num(r["vin"]) > 0]
    D["dc_neg"] = [r for r in dc_rows if _num(r["vin"]) < 0]
    api.log("取数完成: " + ", ".join(f"{k}={len(v)}" for k, v in D.items()))

    test_date = _parse_date(api.latest("bandwidth", "date"))
    date_file = test_date.strftime("%d%m%Y")
    cer_no = str(derived["cer_no"])

    plan = [
        ("data_report", _tpl("test_report", derived), _render_data, False, False, None),
        ("cal_report", _tpl("cal_report", derived), _render_cal, True, True, "calibrationReport"),
        ("certificate", "CalCert.xlsx", _render_cert, True, True, "calibrationCertificate"),
    ]
    outputs = []
    for rtype, tpl_name, render, pdf, mes, mes_field in plan:
        workbook = openpyxl.load_workbook(api.template(tpl_name))
        sheet = workbook.worksheets[0]
        failed = render(api, sheet, D, derived, test_date, version)
        filename = f"Tektronix_{model}_{sn}_{cer_no}_{REPORT_CODE[rtype]}_{date_file}.xlsx"
        path = Path(api.work_dir) / filename
        workbook.save(path)
        outputs.append({
            "type": rtype, "filename": filename, "path": str(path),
            "pdf": pdf, "mes": mes, "mes_field": mes_field, "failed_items": failed,
        })
        api.log(f"{rtype}: {filename}（失败项 {len(failed)}）")
    return outputs
