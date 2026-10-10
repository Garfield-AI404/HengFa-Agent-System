---
skill_key: SK.tax_estimate
title: 当期增值税估算（含附加税）
version: 2026-09-19.1
index: 用户想知道当期大概要交多少增值税及附加税（按 ERP 实际收支流水估算）时使用：先分含税/不含税/未知客户取钣金收款，再取进项，最后用固定工具渲染估算底稿
policies:
tools: erp_calc, erp_query, list_entities, render_tax_worksheet
when_not: 只要某一项（单看销项、单看进项、单看某客户收款）——直接查算回答，不必走本流程；企业所得税（本流程不含）——本流程只估增值税与附加税；报税单生成（本流程不含）；任何写入（建单、改单、考勤写入、收支写入——本流程全程只读）
native_tools:
---

1. 定时段：先把用户要的时段定成闭区间起止日（含首尾）；用户只说「上月」「8 月」时按自然月取整月。时段没定完不进入取数。本流程全程只读，不写任何数据。
2. 取当期有钣金收款的客户：调用 erp_calc，参数 entity=transactions、filter={"tx_type": "收入", "category": "钣金收款", "tx_date": {"between": ["时段起始日", "时段截止日"]}}、group_by=customer_id、measures=[{"field": "amount", "op": "sum"}]。类目名以 ERP 实际名称为准。**「钣金收款」是唯一计入的收入类目，「其余收款」不取（一分钱不计、也不出任何展示行）。** 记下出现的每个 customer_id。
3. 查客户主数据：用第 2 步出现的 customer_id 调用 erp_query，参数 entity=customers、fields=["id", "company_name", "price_type"]，取回每个客户的名称与含税口径。
4. 按 price_type 分三桶：price_type=含税 的客户是含税客户、归入销项桶，其收款进入算税；price_type=不含税 的客户是不含税客户、归入未计税桶，其收款只列、不计税；**客户主数据里查不到记录（customer_id 为空或查无此客户）的归入「未知桶」，其收款只列、不计税，不得静默丢弃。**
5. 取销项桶：调用 erp_calc，参数 entity=transactions、filter={"tx_type": "收入", "category": "钣金收款", "customer_id": {"in": [含税桶客户 id]}, "tx_date": {"between": ["时段起始日", "时段截止日"]}}、group_by=customer_id、measures=[{"field": "amount", "op": "sum"}]。**合计一律用返回的 totals，不要自己把各行相加**（已实测过模型自加会多算）。再用第 3 步的名称映射给每个桶补 company_name 作为行名。
6. 取未计税桶：调用 erp_calc，参数与第 5 步同，只把 customer_id 的取值换成不含税桶客户 id。这一步只为出展示行，金额**不参与任何算税**。
7. 取未知桶：调用 erp_calc，参数与第 5 步同，只把 customer_id 的取值换成未知桶客户 id（含 customer_id 为空的记录）。这一步只为出展示行，金额**不参与任何算税**。
8. 取进项：调用 erp_calc，参数 entity=transactions、filter={"tx_type": "支出", "category": {"in": ["塑粉付款", "电费"]}, "tx_date": {"between": ["时段起始日", "时段截止日"]}}、group_by=category、measures=[{"field": "amount", "op": "sum"}]，得到 13% 档两类；再单独调用一次取 category=天然气 那一档（9% 档）。**本流程进项只取「塑粉付款」「电费」「天然气」三个类目，其它任何支出类目一律不取、跳过、不询问用户。** 类目名以 ERP 实际名称为准。每次取数的合计一律用返回的 totals，不要自己相加。
9. 出底稿：五块都取完后调用 render_tax_worksheet，把五块的 rows 与 totals 传进去（销项桶、未计税桶、未知桶各自按客户，13% 档与 9% 档各自按类目）。**不要自己算任何税额、不要自己相加**——税率与算式全部在工具里。**拿到返回的 text 后必须原样输出给用户**：不得改写、不得增删表格行、不得自己重新排版、不得只输出一部分。**第 9 步是必须执行的一步：无论当期数据是否为空，都必须调用 render_tax_worksheet 并把返回的 text 原样输出——「数据为空」「没有数据」「无记录」都不是跳过第 9 步的理由。**
10. 收尾：明确告知这是**估算值、以代账实际申报为准**；并说明本流程**不含企业所得税**、**不含报税单生成**。
