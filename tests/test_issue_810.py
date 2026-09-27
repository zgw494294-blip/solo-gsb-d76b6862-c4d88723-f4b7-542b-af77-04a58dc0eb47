"""POST /v1/issue-810 端到端测试。"""

import pytest

from fastapi.testclient import TestClient

from app.main import app
from app.issuer import (
    COMPONENT_SEP,
    ELEMENT_SEP,
    SEGMENT_TERM,
)

client = TestClient(app)


def req(**over):
    body = {
        "isa05": "ZZ", "isa06": "SENDER123456789",
        "isa07": "ZZ", "isa08": "RECEIVER1234567",
        "gs02": "S", "gs03": "R",
        "isa09": "200830", "isa10": "1200",
        "gs04": "20200830", "gs05": "1200",
        "big01": "20200830",
        "isa13": "000000001", "gs06": "42", "st02": "0001",
        "big02": "INV-2020-0830",
        "lines": [
            {"it101": "1", "it102": "10", "it104": "19.99"},
            {"it101": "2", "it102": "3", "it104": "0.05"},
        ],
    }
    body.update(over)
    return client.post("/v1/issue-810", json=body)


def post(raw_body):
    return client.post(
        "/v1/issue-810", content=raw_body,
        headers={"Content-Type": "application/json"})


def _expected_cents(qty: str, price: str) -> int:
    from decimal import Decimal, ROUND_HALF_UP, localcontext
    with localcontext() as ctx:
        ctx.prec = 200
        return int((Decimal(qty) * Decimal(price)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


# ---------- 成功路径：报文结构与配对 ----------

def test_happy_path_and_fixed_separators():
    r = req()
    assert r.status_code == 200, r.text
    edi = r.json()["edi_text"]
    assert r.json()["total_amount"] == 19990 + 15
    tags = [frag.split(ELEMENT_SEP)[0]
            for frag in edi.split(SEGMENT_TERM) if frag]
    assert tags == ["ISA", "GS", "ST", "BIG", "IT1", "IT1", "TDS",
                    "SE", "GE", "IEA"]
    expected = (
        "ISA*00*          *00*          *ZZ*SENDER123456789*ZZ*"
        "RECEIVER1234567*200830*1200*U*00401*000000001*0*P*:~"
        "GS*IN*S*R*20200830*1200*42*X*004010~"
        "ST*810*0001~"
        "BIG*20200830*INV-2020-0830~"
        "IT1*1*10*EA*19.99~"
        "IT1*2*3*EA*0.05~"
        "TDS*20005~"
        "SE*6*0001~GE*1*42~IEA*1*000000001~"
    )
    assert edi == expected
    assert COMPONENT_SEP == ":"


def test_isa_is_exactly_106_bytes():
    edi = req().json()["edi_text"]
    isa = edi[: edi.index(SEGMENT_TERM) + 1]
    assert len(isa.encode("ascii")) == 106
    # ISA 段含 16 个元素分隔符，偏移 3/104/105 为三种分隔符
    raw = isa.encode("ascii")
    assert raw[3:4] == b"*"
    assert raw[104:105] == b":"
    assert raw[105:106] == b"~"


def test_short_isa_ids_padded_to_15_with_spaces():
    r = req(isa06="ABC", isa08="X")
    assert r.status_code == 200, r.text
    isa = r.json()["edi_text"].split(SEGMENT_TERM)[0]
    fields = isa.split(ELEMENT_SEP)
    assert fields[6] == "ABC            "  # 3 + 12 空格
    assert fields[8] == "X              "  # 1 + 14 空格


def test_output_passes_v1_verify():
    r = req()
    assert r.status_code == 200
    vr = client.post("/v1/verify", json={"edi_text": r.json()["edi_text"]})
    assert vr.status_code == 200, vr.text
    body = vr.json()
    assert body["invoice_number"] == "INV-2020-0830"
    assert body["total_amount"] == 20005
    assert [(l["line_id"], l["quantity"], l["unit_price"], l["line_amount"])
            for l in body["lines"]] == [
        ("1", "10", "19.99", 19990),
        ("2", "3", "0.05", 15),
    ]


@pytest.mark.parametrize("gs06", ["42", "9", "G0000001"])
@pytest.mark.parametrize("st02", ["0001", "TXN"])
def test_control_numbers_paired(gs06, st02):
    r = req(gs06=gs06, st02=st02)
    assert r.status_code == 200, r.text
    edi = r.json()["edi_text"]
    segs = {f.split(ELEMENT_SEP)[0]: f.split(ELEMENT_SEP)
            for f in edi.split(SEGMENT_TERM) if f}
    assert segs["SE"][2] == st02
    assert segs["GE"][2] == gs06
    assert segs["IEA"][2] == "000000001"
    assert segs["SE"][1] == "6"   # ST,BIG,2xIT1,TDS,SE
    assert segs["GE"][1] == "1"
    assert segs["IEA"][1] == "1"


def test_se01_scales_with_line_count():
    n = 5
    r = req(lines=[{"it101": str(i), "it102": "1", "it104": "1"}
                   for i in range(1, n + 1)])
    assert r.status_code == 200, r.text
    edi = r.json()["edi_text"]
    segs = [f.split(ELEMENT_SEP) for f in edi.split(SEGMENT_TERM) if f]
    se = next(f for f in segs if f[0] == "SE")
    assert se[1] == str(n + 4)   # ST,BIG,n×IT1,TDS,SE


def test_datetimes_and_envelope_fields_pass_through():
    r = req(isa09="211231", isa10="2359", gs04="20211231", gs05="2359",
            big01="20211231", gs02="SENDERGS", gs03="RECVGS")
    assert r.status_code == 200, r.text
    edi = r.json()["edi_text"]
    assert "GS*IN*SENDERGS*RECVGS*20211231*2359*42*X*004010~" in edi
    assert "BIG*20211231*INV-2020-0830~" in edi
    assert "*211231*2359*U*00401*" in edi


# ---------- 金额：逐行 ROUND_HALF_UP 与合计 ----------

@pytest.mark.parametrize("qty,price", [
    ("1", "19.99"),
    ("3", "0.05"),
    ("1", "0.005"),       # 半分边界：ROUND_HALF_UP -> 1
    ("0.001", "15"),      # 0.015 -> 2
    ("0.003", "0.005"),   # 0.000015 -> 0
    ("2.5", "2.5"),       # 6.25 -> 625
    ("9.999", "9999.9999"),
])
def test_line_total_matches_reference(qty, price):
    r = req(lines=[{"it101": "1", "it102": qty, "it104": price}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_amount"] == _expected_cents(qty, price)
    vr = client.post("/v1/verify", json={"edi_text": body["edi_text"]})
    assert vr.status_code == 200
    assert vr.json()["lines"][0]["line_amount"] == body["total_amount"]


def test_tds_is_sum_of_per_line_rounded_cents():
    # 各行各自四舍五入后再求和（不是先求和再取整）
    lines = [
        {"it101": "1", "it102": "1", "it104": "0.005"},   # -> 1
        {"it101": "2", "it102": "1", "it104": "0.004"},   # -> 0
        {"it101": "3", "it102": "1", "it104": "0.005"},   # -> 1
    ]
    r = req(lines=lines)
    assert r.status_code == 200
    body = r.json()
    assert body["total_amount"] == 2
    assert "TDS*2~" in body["edi_text"]


def test_100_lines_accepted_and_within_64kib():
    lines = [{"it101": f"L{i:03d}", "it102": "1.001", "it104": "12.3456"}
             for i in range(100)]
    r = req(lines=lines)
    assert r.status_code == 200, r.text
    edi = r.json()["edi_text"]
    assert len(edi.encode("ascii")) <= 64 * 1024
    vr = client.post("/v1/verify", json={"edi_text": edi})
    assert vr.status_code == 200
    assert len(vr.json()["lines"]) == 100


# ---------- 非法输入：422 带 field 与 code ----------

def _expect_422(r, code, field):
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == code
    assert err["field"] == field
    assert err["message"]


@pytest.mark.parametrize("field,value,code", [
    ("isa05", "Z", "ISSUE_QUALIFIER_INVALID"),
    ("isa05", "ZZZ", "ISSUE_TOO_LONG"),
    ("isa05", "*Z", "ISSUE_SEPARATOR_INJECTION"),
    ("isa07", "Z", "ISSUE_QUALIFIER_INVALID"),
    ("isa07", "ZZZ", "ISSUE_TOO_LONG"),
    ("isa06", "A" * 16, "ISSUE_TOO_LONG"),
    ("isa06", "", "ISSUE_EMPTY"),
    ("isa08", "S*NDER", "ISSUE_SEPARATOR_INJECTION"),
    ("isa08", "SENDERÄ", "ISSUE_NOT_ASCII"),
    ("gs02", "A" * 16, "ISSUE_TOO_LONG"),
    ("gs02", "AB\x01", "ISSUE_CONTROL_CHAR"),
    ("gs03", "", "ISSUE_EMPTY"),
    ("gs06", "", "ISSUE_EMPTY"),
    ("gs06", "A" * 10, "ISSUE_TOO_LONG"),
    ("gs06", "4*2", "ISSUE_SEPARATOR_INJECTION"),
    ("st02", "", "ISSUE_EMPTY"),
    ("st02", "A:B", "ISSUE_SEPARATOR_INJECTION"),
    ("big02", "", "ISSUE_EMPTY"),
    ("big02", "A" * 23, "ISSUE_TOO_LONG"),
    ("big02", "INV~1", "ISSUE_SEPARATOR_INJECTION"),
    ("big02", "发票", "ISSUE_NOT_ASCII"),
])
def test_scalar_field_rejections(field, value, code):
    r = req(**{field: value})
    _expect_422(r, code, field)


@pytest.mark.parametrize("field,value,code", [
    ("isa07", "", "ISSUE_EMPTY"),
    ("isa05", "ÄZ", "ISSUE_NOT_ASCII"),
])
def test_qualifier_extra_rejections(field, value, code):
    _expect_422(req(**{field: value}), code, field)


@pytest.mark.parametrize("field,value,code", [
    ("isa13", "00000001", "ISSUE_ISA13_INVALID"),
    ("isa13", "00000000A", "ISSUE_ISA13_INVALID"),
    ("isa13", "1234567890", "ISSUE_TOO_LONG"),
    ("isa13", "", "ISSUE_EMPTY"),
])
def test_isa13_rejections(field, value, code):
    r = req(isa13=value)
    _expect_422(r, code, "isa13")


def test_isa13_given_number_not_coerced():
    r = req(isa13=123)
    _expect_422(r, "ISSUE_NOT_STRING", "isa13")


@pytest.mark.parametrize("field,value", [
    ("isa09", "20083"),
    ("isa09", "20083X"),
    ("isa10", "120"),
    ("isa10", "12:00"),
    ("gs04", "2020-08-30"),
    ("gs04", "2020083"),
    ("gs05", "abcd"),
    ("big01", "202008301"),
])
def test_datetime_rejections(field, value):
    r = req(**{field: value})
    err = r.json()["error"]
    assert r.status_code == 422
    assert err["field"] == field
    assert err["code"] in ("ISSUE_DATETIME_INVALID", "ISSUE_TOO_LONG",
                           "ISSUE_SEPARATOR_INJECTION")


def test_missing_top_level_field_422():
    r = client.post("/v1/issue-810", json={
        "isa05": "ZZ", "isa06": "S", "isa07": "ZZ", "isa08": "R",
        "gs02": "S", "gs03": "R",
        "isa09": "200830", "isa10": "1200",
        "gs04": "20200830", "gs05": "1200", "big01": "20200830",
        "isa13": "000000001", "gs06": "42",
        "big02": "I", "lines": [],
    })
    _expect_422(r, "ISSUE_MISSING", "st02")


def test_non_json_body_422():
    r = post(b"{not json")
    _expect_422(r, "ISSUE_INVALID_JSON", None)


def test_body_not_object_422():
    r = post(b"[1,2,3]")
    assert r.status_code == 422
    assert r.json()["error"]["field"] is None


def test_string_field_given_number_422():
    # 字符串字段收到 JSON 数字：Pydantic 拒绝（lax 模式不把 int 当 str），
    # 归一为 ISSUE_NOT_STRING 422
    body = _default_body()
    body["isa13"] = 123
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_NOT_STRING", "isa13")


def _default_body():
    return {
        "isa05": "ZZ", "isa06": "SENDER123456789",
        "isa07": "ZZ", "isa08": "RECEIVER1234567",
        "gs02": "S", "gs03": "R",
        "isa09": "200830", "isa10": "1200",
        "gs04": "20200830", "gs05": "1200", "big01": "20200830",
        "isa13": "000000001", "gs06": "42", "st02": "0001",
        "big02": "INV", "lines": [
            {"it101": "1", "it102": "1", "it104": "1"}],
    }


def test_lines_not_array_422():
    body = _default_body()
    body["lines"] = {"it101": "1"}
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_NOT_ARRAY", "lines")


def test_line_object_not_object_422():
    body = _default_body()
    body["lines"] = ["x"]
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_INVALID_OBJECT", "lines.0")


def test_missing_line_field_422():
    body = _default_body()
    del body["lines"][0]["it102"]
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_MISSING", "lines.0.it102")


def test_no_lines_and_too_many_lines():
    r = req(lines=[])
    _expect_422(r, "ISSUE_LINES_TOO_FEW", "lines")

    r = req(lines=[{"it101": str(i), "it102": "1", "it104": "1"}
                   for i in range(101)])
    _expect_422(r, "ISSUE_LINES_TOO_MANY", "lines")


def test_duplicate_line_number_rejected():
    r = req(lines=[
        {"it101": "7", "it102": "1", "it104": "1"},
        {"it101": "8", "it102": "1", "it104": "1"},
        {"it101": "7", "it102": "1", "it104": "1"},
    ])
    _expect_422(r, "ISSUE_IT101_DUPLICATE", "lines[2].it101")


def test_empty_line_id_rejected():
    body = _default_body()
    body["lines"][0]["it101"] = ""
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_EMPTY", "lines[0].it101")


def test_line_id_too_long():
    body = _default_body()
    body["lines"][0]["it101"] = "A" * 21
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_TOO_LONG", "lines[0].it101")


@pytest.mark.parametrize("qty,code", [
    ("0", "ISSUE_IT102_INVALID"),
    ("-1", "ISSUE_IT102_INVALID"),
    ("1.0001", "ISSUE_IT102_INVALID"),
    ("abc", "ISSUE_IT102_INVALID"),
    ("1.", "ISSUE_IT102_INVALID"),
    (".5", "ISSUE_IT102_INVALID"),
    ("12345678901", "ISSUE_TOO_LONG"),
])
def test_it102_rejections(qty, code):
    body = _default_body()
    body["lines"][0]["it102"] = qty
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, code, "lines[0].it102")


@pytest.mark.parametrize("price,code", [
    ("-0.01", "ISSUE_IT104_INVALID"),
    ("0.00001", "ISSUE_IT104_INVALID"),
    ("abc", "ISSUE_IT104_INVALID"),
    (".9", "ISSUE_IT104_INVALID"),
    ("123456789012345", "ISSUE_TOO_LONG"),
])
def test_it104_rejections(price, code):
    body = _default_body()
    body["lines"][0]["it104"] = price
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, code, "lines[0].it104")


@pytest.mark.parametrize("price", ["0", "0.0", "0.0000"])
def test_it104_zero_accepted(price):
    body = _default_body()
    body["lines"][0]["it104"] = price
    r = client.post("/v1/issue-810", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["total_amount"] == 0
    assert "TDS*0~" in r.json()["edi_text"]


def test_line_field_separator_injection():
    body = _default_body()
    body["lines"][0]["it101"] = "L*1"
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_SEPARATOR_INJECTION", "lines[0].it101")


def test_non_ascii_decimal_rejected():
    body = _default_body()
    body["lines"][0]["it104"] = "１.５"  # 全角数字
    r = client.post("/v1/issue-810", json=body)
    _expect_422(r, "ISSUE_NOT_ASCII", "lines[0].it104")


def test_other_endpoint_keeps_default_validation_error():
    # 自定义 RequestValidationError 处理器不得影响其它接口的默认 422 结构
    r = client.post("/v1/verify", json={})
    assert r.status_code == 422
    assert "detail" in r.json()
