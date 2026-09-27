"""结构化发票数据 -> ASC X12 004010 810（Invoice）报文生成。

入口：issue_810(...) -> IssueInvoice(edi_text, total_amount)。

固定使用元素分隔符 ``*``、组件分隔符 ``:``、段终止符 ``~``；ISA 段为
固定 106 字节（ISA06/ISA08 左对齐并以空格补足 15 字符）。报文外壳顺序与
``verifier`` 完全一致：

    ISA -> GS -> ST -> BIG -> IT1(1..100) -> TDS -> SE -> GE -> IEA

其中 ISA01..04/11/12/14/15 固定（00 / 空授权信息 / U / 00401 / 0 / P），
GS01=IN、GS07=X、GS08=004010，IT103 固定为 ``EA``；ISA13=IEA02、
GS06=GE02、ST02=SE02 两两配对，SE01/GE01/IEA01 计数自洽。

金额规则与 ``verifier.verify`` 逐字节对齐：IT102（正，至多 3 位小数）*
IT104（非负，至多 4 位小数）逐行以整数定点数精确相乘，按 ROUND_HALF_UP
取整到整数分（复用 verifier 的 ``_line_to_cents``），各行整数分之和
写入 TDS01（N2 隐含两位小数，即直接写整数分）。生成后再以
``verify_text`` 自检，保证输出可通过 ``POST /v1/verify`` 且不超过 64 KiB。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .verifier import (
    MAX_BYTES,
    MAX_IT1,
    _FixedPoint,
    _QTY_RE,
    _UNIT_RE,
    _line_to_cents,
    verify_text,
)

# ---- 固定分隔符与信封常量 ------------------------------------------------
ELEMENT_SEP = "*"
COMPONENT_SEP = ":"
SEGMENT_TERM = "~"

ISA_ID_WIDTH = 15          # ISA06/ISA08 发送方/接收方标识定宽 15
ISA_AUTH_WIDTH = 10        # ISA02/ISA04 空授权/安全信息（10 空格）

# X12 004010 字段长度上限
MAX_QUALIFIER = 2          # ISA05/ISA07
MAX_ISA_ID = 15            # ISA06/ISA08
MAX_GS_ID = 15             # GS02/GS03
MAX_CONTROL = 9            # GS06/ST02
MAX_BIG02 = 22             # BIG02 发票号
MAX_IT101 = 20             # IT101 行号
MAX_IT102 = 10             # IT102 数量（X12 R 型，1..10）
MAX_IT104 = 14             # IT104 单价（X12 R 型，1..14）

# 日期/时间格式：ISA09 YYMMDD、ISA10/GS05 HHMM、GS04/BIG01 CCYYMMDD
_ISA09_RE = re.compile(r"^\d{6}$")
_HHMM_RE = re.compile(r"^\d{4}$")
_CCYYMMDD_RE = re.compile(r"^\d{8}$")
_NINE_DIGITS_RE = re.compile(r"^\d{9}$")

_SEPARATORS = (ELEMENT_SEP, COMPONENT_SEP, SEGMENT_TERM)


class IssueError(Exception):
    """结构化输入非法；HTTP 固定 422，带字段名与错误码。"""

    def __init__(self, code: str, field: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


@dataclass(frozen=True)
class IssueLine:
    """单行发票数据（IT101 行号 / IT102 数量 / IT104 单价）。"""

    line_id: str
    quantity: str
    unit_price: str


@dataclass(frozen=True)
class IssueInvoice:
    edi_text: str
    total_amount: int       # TDS01 总额，整数分


# ---- 字段校验 -------------------------------------------------------------


def _check_text(
    value: str,
    field: str,
    *,
    max_len: int,
    label: str,
) -> None:
    """通用文本字段校验：非空 -> 纯 ASCII -> 无分隔符 -> 无控制字符 -> 长度。"""
    if value == "":
        raise IssueError("ISSUE_EMPTY", field, f"{label}不能为空")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise IssueError(
            "ISSUE_NOT_ASCII", field, f"{label}必须为纯 ASCII: {value!r}")
    if any(ch in value for ch in _SEPARATORS):
        raise IssueError(
            "ISSUE_SEPARATOR_INJECTION", field,
            f"{label}不得包含固定分隔符 '*'、':'、'~': {value!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise IssueError(
            "ISSUE_CONTROL_CHAR", field,
            f"{label}不得包含 ASCII 控制字符: {value!r}")
    if len(value) > max_len:
        raise IssueError(
            "ISSUE_TOO_LONG", field,
            f"{label}长度 {len(value)} 超过上限 {max_len}: {value!r}")


def _check_pattern(value: str, field: str, pattern: re.Pattern[str],
                   label: str, expected: str) -> None:
    if not pattern.fullmatch(value):
        raise IssueError(
            "ISSUE_DATETIME_INVALID", field,
            f"{label}必须为{expected}: {value!r}")


def _parse_decimal(raw: str, field: str, pattern: re.Pattern[str],
                   code: str, label: str) -> _FixedPoint:
    m = pattern.fullmatch(raw)
    if not m:
        raise IssueError(
            code, field, f"{label}不是合法十进制数: {raw!r}")
    whole, frac = m.group(1), m.group(2) or ""
    return _FixedPoint(int(whole + frac) if frac else int(whole), len(frac))


def issue_810(
    *,
    isa05: str,
    isa06: str,
    isa07: str,
    isa08: str,
    gs02: str,
    gs03: str,
    isa09: str,
    isa10: str,
    gs04: str,
    gs05: str,
    big01: str,
    isa13: str,
    gs06: str,
    st02: str,
    big02: str,
    lines: list[IssueLine],
) -> IssueInvoice:
    """校验结构化发票数据并生成自洽的 004010 X12 810 报文；非法输入抛 IssueError。"""

    # ---- ISA 收发方限定符/标识 ------------------------------------------
    for field, value, label in (
        ("isa05", isa05, "ISA05 发送方限定符"),
        ("isa07", isa07, "ISA07 接收方限定符"),
    ):
        _check_text(value, field, max_len=MAX_QUALIFIER, label=label)
        if len(value) != MAX_QUALIFIER:
            raise IssueError(
                "ISSUE_QUALIFIER_INVALID", field,
                f"{label}必须为恰好 {MAX_QUALIFIER} 个字符: {value!r}")

    for field, value, label in (
        ("isa06", isa06, "ISA06 发送方标识"),
        ("isa08", isa08, "ISA08 接收方标识"),
        ("gs02", gs02, "GS02 应用发送方代码"),
        ("gs03", gs03, "GS03 应用接收方代码"),
    ):
        _check_text(value, field, max_len=MAX_ISA_ID
                    if field.startswith("isa") else MAX_GS_ID, label=label)

    # ---- 日期时间 --------------------------------------------------------
    for value, field, pattern, label, length, expected in (
        (isa09, "isa09", _ISA09_RE, "ISA09 交换日期", 6, "6 位数字 YYMMDD"),
        (isa10, "isa10", _HHMM_RE, "ISA10 交换时间", 4, "4 位数字 HHMM"),
        (gs04, "gs04", _CCYYMMDD_RE, "GS04 功能组日期", 8,
         "8 位数字 CCYYMMDD"),
        (gs05, "gs05", _HHMM_RE, "GS05 功能组时间", 4, "4 位数字 HHMM"),
        (big01, "big01", _CCYYMMDD_RE, "BIG01 发票日期", 8,
         "8 位数字 CCYYMMDD"),
    ):
        _check_text(value, field, max_len=length, label=label)
        _check_pattern(value, field, pattern, label, expected)

    # ---- 控制号 / 发票号 --------------------------------------------------
    _check_text(isa13, "isa13", max_len=9, label="ISA13 交换控制号")
    if not _NINE_DIGITS_RE.fullmatch(isa13):
        raise IssueError(
            "ISSUE_ISA13_INVALID", "isa13",
            f"ISA13 交换控制号必须为恰好 9 位数字: {isa13!r}")
    _check_text(gs06, "gs06", max_len=MAX_CONTROL, label="GS06 组控制号")
    _check_text(st02, "st02", max_len=MAX_CONTROL,
                label="ST02 交易集控制号")
    _check_text(big02, "big02", max_len=MAX_BIG02, label="BIG02 发票号")

    # ---- 发票行 ----------------------------------------------------------
    if not lines:
        raise IssueError("ISSUE_LINES_TOO_FEW", "lines",
                         f"至少需要 1 行 IT1，实际 0 行")
    if len(lines) > MAX_IT1:
        raise IssueError(
            "ISSUE_LINES_TOO_MANY", "lines",
            f"IT1 行数 {len(lines)} 超过上限 {MAX_IT1}")

    seen_ids: set[str] = set()
    line_cents: list[int] = []
    for pos, line in enumerate(lines):
        f_id = f"lines[{pos}].it101"
        f_qty = f"lines[{pos}].it102"
        f_price = f"lines[{pos}].it104"

        _check_text(line.line_id, f_id, max_len=MAX_IT101,
                    label=f"第 {pos + 1} 行 IT101 行号")
        if line.line_id in seen_ids:
            raise IssueError(
                "ISSUE_IT101_DUPLICATE", f_id,
                f"IT101 行号重复: {line.line_id!r}")
        seen_ids.add(line.line_id)

        # 数量/单价为 ASCII 十进制（先做非 ASCII/控制字符预检，
        # 避免 Python \d 匹配到 Unicode 数字）
        _check_text(line.quantity, f_qty, max_len=MAX_IT102,
                    label=f"第 {pos + 1} 行 IT102 数量")
        qty = _parse_decimal(line.quantity, f_qty, _QTY_RE,
                             "ISSUE_IT102_INVALID", "IT102 数量")
        if qty.mantissa <= 0:
            raise IssueError(
                "ISSUE_IT102_INVALID", f_qty,
                f"IT102 数量必须为正数（至多 3 位小数）: "
                f"{line.quantity!r}")

        _check_text(line.unit_price, f_price, max_len=MAX_IT104,
                    label=f"第 {pos + 1} 行 IT104 单价")
        unit = _parse_decimal(line.unit_price, f_price, _UNIT_RE,
                              "ISSUE_IT104_INVALID", "IT104 单价")
        # 非负：_UNIT_RE 不含符号，mantissa 恒 >= 0（0 价格合法）

        line_cents.append(_line_to_cents(qty, unit))

    # ---- 组装报文 --------------------------------------------------------
    edi_text = _build_segments(
        isa05=isa05, isa06=isa06, isa07=isa07, isa08=isa08,
        gs02=gs02, gs03=gs03, isa09=isa09, isa10=isa10,
        gs04=gs04, gs05=gs05, big01=big01,
        isa13=isa13, gs06=gs06, st02=st02, big02=big02,
        lines=lines, line_cents=line_cents)

    total = sum(line_cents)

    # ---- 自检：≤64 KiB 且可通过 /v1/verify -------------------------------
    if len(edi_text.encode("ascii")) > MAX_BYTES:
        raise IssueError(
            "ISSUE_PAYLOAD_TOO_LARGE", None,
            f"生成报文超过 {MAX_BYTES} 字节上限")
    invoice = verify_text(edi_text)
    assert invoice.total_amount == total, (invoice.total_amount, total)
    return IssueInvoice(edi_text=edi_text, total_amount=total)


def _seg(tag: str, *values: str) -> str:
    return ELEMENT_SEP.join([tag, *values]) + SEGMENT_TERM


def _build_segments(
    *,
    isa05: str,
    isa06: str,
    isa07: str,
    isa08: str,
    gs02: str,
    gs03: str,
    isa09: str,
    isa10: str,
    gs04: str,
    gs05: str,
    big01: str,
    isa13: str,
    gs06: str,
    st02: str,
    big02: str,
    lines: list[IssueLine],
    line_cents: list[int],
) -> str:
    # ISA 16 个元素：ISA01/03 固定 "00"，ISA02/04 为 10 空格；
    # ISA11="U"、ISA12="00401"、ISA14="0"、ISA15="P"，ISA16 即组件分隔符。
    isa_fields = [
        "00", " " * ISA_AUTH_WIDTH,   # ISA01/02
        "00", " " * ISA_AUTH_WIDTH,   # ISA03/04
        isa05, isa06.ljust(ISA_ID_WIDTH),   # ISA05/06
        isa07, isa08.ljust(ISA_ID_WIDTH),   # ISA07/08
        isa09, isa10,                  # ISA09/10
        "U", "00401",                  # ISA11/12
        isa13,                         # ISA13
        "0",                           # ISA14
        "P",                           # ISA15
        COMPONENT_SEP,                 # ISA16（组件分隔符，不计入 join 长度）
    ]
    isa_segment = "ISA" + ELEMENT_SEP + ELEMENT_SEP.join(isa_fields) \
        + SEGMENT_TERM
    assert len(isa_segment) == 106, len(isa_segment)

    out: list[str] = [
        isa_segment,
        _seg("GS", "IN", gs02, gs03, gs04, gs05, gs06, "X", "004010"),
        _seg("ST", "810", st02),
        _seg("BIG", big01, big02),
    ]
    for line, _cents in zip(lines, line_cents):
        out.append(_seg("IT1", line.line_id, line.quantity, "EA",
                        line.unit_price))
    out.append(_seg("TDS", str(sum(line_cents))))

    # SE01 = ST、BIG、n×IT1、TDS、SE 的段数 = 3 + n + 1 = n + 4
    se_count = len(lines) + 4
    out.append(_seg("SE", str(se_count), st02))
    out.append(_seg("GE", "1", gs06))
    out.append(_seg("IEA", "1", isa13))
    return "".join(out)
