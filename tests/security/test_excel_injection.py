"""Excel 单元格注入防护（规格 14.4）。

## 2026-08-29 说明

本文件从 `tests/security/test_injection.py` 救回。原文件 17 例中：

- 12 例（`TestPolicyKeyNotUserControllable` 6 例 +
  `TestForgedIntentFailsClosed` 6 例）依赖 D26 已删的
  `agent/intent.py`（`validate_intent` / `policy_key_for` /
  `RuleBasedIntentClassifier`…）。其守护的不变量是「policy_key 只由
  代码枚举派生、外部输入无法指定」——D26 改为 LLM 直接决策后，
  `policy_key` 这一中间产物本身已不存在，**无重写对象**，随文件删除。
- 5 例（`TestExcelCellInjection`）只依赖仍存活的
  `files/excel_parser.convert_cell`，与已删模块无关。其中 4 例在本文件
  原样保留；第 5 例 `test_cell_text_never_reaches_policy_selection`
  仍依赖 `IntentResult` / `policy_key_for`，不可救，一并删除。

为何值得单独保留：`tests/domains/test_excel_parser.py`（277 行）覆盖的
是**转换语义**（类型转换、必填、行号、fail closed 的未知 dataType/rule），
与「注入」零交集——不含任何 formula / hyperlink / credential 场景。
本文件守的是另一条线：**单元格文本只作为数据值，永不产生执行路径**。

新增 2 例（#5、#6）覆盖 CSV/Excel 公式注入的常见前缀 `+ - @ |`，
原文件未覆盖，属同一不变量的补强，不改动任何生产代码。
"""

from __future__ import annotations

from administrative_agent.files.excel_parser import convert_cell


class TestExcelCellInjection:
    """14.4：Excel 单元格注入文本只作为数据值，不产生执行路径。"""

    def test_formula_cell_is_data_value_not_code(self) -> None:
        """`=1+1` 原样作为字符串数据，不被求值。"""
        value, error = convert_cell(
            data_type="string", cell="=1+1", enum_mapping=None, conversion_rule=None,
        )
        assert (value, error) == ("=1+1", None)  # 原样作为数据，无计算/执行

    def test_hyperlink_formula_cell_kept_as_text(self) -> None:
        """=HYPERLINK(...) 钓鱼载荷保持为纯文本。"""
        cell = '=HYPERLINK("http://evil.example","click")'
        value, error = convert_cell(
            data_type="string", cell=cell, enum_mapping=None, conversion_rule=None,
        )
        assert (value, error) == (cell, None)

    def test_injected_policy_key_cell_fails_enum_conversion(self) -> None:
        """枚举单元格注入 policy_key 文本 → 不在映射中 → fail closed。"""
        value, error = convert_cell(
            data_type="enum", cell="=R6.reconciliation.execute",
            enum_mapping={"金属": "metal"}, conversion_rule=None,
        )
        assert value is None
        assert error is not None
        assert "=R6.reconciliation.execute" in error

    def test_injected_credential_cell_is_plain_string_data(self) -> None:
        """单元格里长得像密钥的文本只当普通数据处理（不触发告警也不外泄）。"""
        value, error = convert_cell(
            data_type="string", cell="sk-abcdefghijklmnopqrst",
            enum_mapping=None, conversion_rule=None,
        )
        assert (value, error) == ("sk-abcdefghijklmnopqrst", None)  # 仅数据

    def test_alternative_formula_prefixes_are_plain_data(self) -> None:
        """`+ - @ |` 前缀同样是公式注入入口（CSV 导出场景常见），一律当数据。

        之所以逐个断言：这四种前缀在 Excel/LibreOffice 中都会被当作公式
        起始符，而它们不像 `=` 那样显眼，容易被漏掉。
        """
        for payload in ("+1+1", "-1+1", "@SUM(1+1)", "|1+1"):
            value, error = convert_cell(
                data_type="string", cell=payload,
                enum_mapping=None, conversion_rule=None,
            )
            assert (value, error) == (payload, None), f"公式前缀注入被处理：{payload}"

    def test_trim_rule_does_not_neutralise_or_evaluate_formula(self) -> None:
        """trim 只去首尾空白，既不求值也不改写公式本体。"""
        value, error = convert_cell(
            data_type="string", cell="  =1+1  ",
            enum_mapping=None, conversion_rule="trim",
        )
        assert (value, error) == ("=1+1", None)
