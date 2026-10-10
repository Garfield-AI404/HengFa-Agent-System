"""agent/prompts：固定基础 System Prompt（base）、版本化规则包（policies）。

10.1/10.2 两层结构。

本体只有代码：固定基础正文在 ``base_prompt.md``、规则包正文在
``policy_texts/*.md``、skill 正文在 ``skill_texts/*.md``，三者都在 import 期
由各自的加载器读取并做 fail-closed 校验（缺文件/缺段/格式错 → 直接抛）。
**改正文改 md，不改代码。**
"""
