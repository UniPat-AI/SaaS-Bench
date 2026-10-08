**任务要求：**

执行一条覆盖 CRM 管道分析、BigCapital 财务报表与应收账款龄期跟踪、Frappe HRMS 考勤与费用验证，以及 Pretix 活动票务表现复盘的季度末运营审查。审查范围为 2026-07-01 到 2026-09-30。

在 BigCapital 中（先完成这些步骤以推导逾期客户名称）：(6) 打开 Profit and Loss Sheet，筛选日期范围 2026-07-01 到 2026-09-30，记账基础为 'Accrual'。将报告导出为 PDF。将总收入记录为 PL_REVENUE，总费用记录为 PL_EXPENSES，净利润记录为 PL_NET_INCOME。 (7) 打开 Balance Sheet，筛选截至日期 2026-09-30，记账基础为 'Accrual'。将报告导出为 PDF。将总资产记录为 BS_ASSETS，总负债记录为 BS_LIABILITIES，总权益记录为 BS_EQUITY。 (8) 打开 A/R Aging Summary，筛选截至日期 2026-09-30。将报告导出为 PDF。将总应收账款记录为 AR_TOTAL。识别在 61-90 天或 90+ 天账龄区间中余额非零的每一个唯一客户名称。将这些名称按其在报告中的原样记录下来——该推导出的列表在后续步骤中称为 OVERDUE_CLIENTS。 (9) 打开 A/P Aging Summary，筛选截至日期 2026-09-30。将报告导出为 PDF。将总应付账款记录为 AP_TOTAL。 (10) 打开 Cash Flow Statement，筛选日期范围 2026-07-01 到 2026-09-30。将报告导出为 PDF。将经营活动净现金流记录为 CASH_OPS。 (11) 打开 Sales by Items，筛选日期范围 2026-07-01 到 2026-09-30。将报告导出为 PDF。将按收入计算的畅销项目记录为 TOP_ITEM，并将其总额记录为 TOP_ITEM_REVENUE。

在 Twenty CRM 中：(1) 打开 Opportunities 列表。使用筛选栏添加条件 Stage = 'Won' 且 Close Date 在 2026-07-01 到 2026-09-30 之间（如果没有精确的日期范围筛选，则先按 Stage = 'Won' 筛选，再手动识别关闭日期落在该季度内的商机）。按金额降序排序。记录 Won 成交的总数量及其总收入——分别称为 WON_COUNT 和 WON_REVENUE。 (2) 使用筛选栏添加条件 Stage = 'Lost' 且 Close Date 在 2026-07-01 到 2026-09-30 之间（采用与步骤 1 相同的筛选方式）。将数量记录为 LOST_COUNT。 (3) 按阶段 'SCREENING'（开放管道）筛选商机。按金额降序排序。记录总数量和总金额，分别为 OPEN_COUNT 和 OPEN_PIPELINE。 (4) 对于从 BigCapital A/R Aging Summary 推导出的 OVERDUE_CLIENTS 列表中的每一个唯一客户名称，在 Twenty Companies 列表中搜索匹配公司。如果找到精确匹配，则打开公司详情页并创建一个关联到该公司的任务，标题为 'Follow up on overdue receivable -- [client name]'，到期日为 2026-10-15，正文为：'Overdue balance identified in Q3 aged receivables review. Contact accounts payable to arrange payment. Review deadline: 2026-10-15.' 如果在 Twenty 中找不到精确匹配的公司，则创建一个不关联任何公司记录的独立任务，使用相同的标题、到期日和正文，并将客户名称替换为其在 BigCapital A/R Aging Summary 报告中出现的原始名称。 (5) 按公式 WON_COUNT / (WON_COUNT + LOST_COUNT) * 100 计算胜率，并四舍五入到小数点后一位。如果 WON_COUNT + LOST_COUNT 等于 0，则将胜率记为 'N/A'。创建一个标题为 'Q3 Pipeline Summary -- 2026-09-30' 的笔记，正文为：'Won deals: WON_COUNT, total revenue: WON_REVENUE USD
Lost deals: LOST_COUNT
Open pipeline (SCREENING): OPEN_COUNT deals, OPEN_PIPELINE USD
Win rate: [computed value]%
Overdue clients flagged: [comma-separated OVERDUE_CLIENTS list]
Follow-up tasks created with due date 2026-10-15'

在 Frappe HRMS 中：(12) 打开 2026 年 9 月的 Monthly Attendance Sheet 报告。如果可用，请将公司筛选设置为 'TechVista Solutions Pvt. Ltd.'；否则使用默认视图。如果报告分页，请遍历所有页面。将列出的员工总数记录为 ATTENDANCE_HEADCOUNT。通过检查每个员工行中的 Absent 列总数，识别缺勤超过 5 天的员工。将这些姓名记录为 HIGH_ABSENCE_EMPLOYEES。如果没有员工超过该阈值，则将 HIGH_ABSENCE_EMPLOYEES 记为 'None'。 (13) 打开 Unpaid Expense Claim 报告。如果报告分页，请遍历所有页面。通过对报告中所有行的 amount 列求和，记录未支付报销的总数量为 UNPAID_CLAIMS_COUNT，未支付总金额为 UNPAID_CLAIMS_TOTAL。 (14) 打开 Employee Leave Balance Summary 报告。如果有请假类型筛选，请将其设为 'Sick Leave'。如果没有请假类型筛选，则扫描报告输出中与 'Sick Leave' 对应的行或列。如果 'Sick Leave' 出现在报告输出中（无论是作为筛选结果还是作为行/列标签），则将该请假类型在所有列出的员工中的总未结余假期记录为 LEAVE_LIABILITY_DAYS。如果 'Sick Leave' 完全未出现在报告输出中，则将 LEAVE_LIABILITY_DAYS 记为 'Not available'。 (15) 打开 Employee Advance Summary 报告。如果报告分页，请遍历所有页面。统计状态为 'Unpaid' 的预支，或显示未结清余额的预支。将数量记录为 OPEN_ADVANCES_COUNT，并通过对相关金额列求和记录未结清总额为 OPEN_ADVANCES_TOTAL。

在 Pretix 中：(16) 打开组织者 'broadway-group' 下活动 'Hamilton' 的 Event Dashboard。将仪表板组件中的总订单数记录为 EVENT_ORDERS，总收入记录为 EVENT_REVENUE。 (17) 打开 'Hamilton' 的 Event Orders Overview 页面。使用订单列表及其产品/项目拆分列，确定可归因于产品 'Balcony' 的收入为 PROD1_REVENUE，以及可归因于产品 'Playbill Program' 的收入为 PROD2_REVENUE。还要使用订单状态筛选或状态列，按状态统计订单：已付款订单为 PAID_ORDERS，待处理订单为 PENDING_ORDERS，已取消订单为 CANCELLED_ORDERS。

在 Twenty CRM 中：(18) 创建一个标题为 'Q3 Operations Review -- Complete -- 2026-09-30' 的笔记，正文为：
'FINANCIAL SUMMARY (Accrual basis):
- P&L: Revenue PL_REVENUE, Expenses PL_EXPENSES, Net Income PL_NET_INCOME
- Balance Sheet: Assets BS_ASSETS, Liabilities BS_LIABILITIES, Equity BS_EQUITY
- Cash Flow from Ops: CASH_OPS
- A/R Outstanding: AR_TOTAL (overdue clients: [comma-separated OVERDUE_CLIENTS list])
- A/P Outstanding: AP_TOTAL
- Top item by sales: TOP_ITEM at TOP_ITEM_REVENUE

CRM PIPELINE:
- Won: WON_COUNT deals, WON_REVENUE USD
- Lost: LOST_COUNT deals
- Open (SCREENING): OPEN_COUNT deals, OPEN_PIPELINE USD
- Win rate: [computed value]%

HR METRICS:
- Headcount (September 2026): ATTENDANCE_HEADCOUNT
- High absence (>5 days): HIGH_ABSENCE_EMPLOYEES
- Unpaid expense claims: UNPAID_CLAIMS_COUNT totaling UNPAID_CLAIMS_TOTAL USD
- Leave liability (Sick Leave): LEAVE_LIABILITY_DAYS days
- Open advances: OPEN_ADVANCES_COUNT totaling OPEN_ADVANCES_TOTAL USD

EVENT PERFORMANCE (Hamilton):
- Total orders: EVENT_ORDERS, revenue: EVENT_REVENUE USD
- Balcony: PROD1_REVENUE USD
- Playbill Program: PROD2_REVENUE USD
- Paid: PAID_ORDERS, Pending: PENDING_ORDERS, Cancelled: CANCELLED_ORDERS'
(19) 创建一个标题为 'Present Q3 operations review to leadership' 的任务，到期日为 2026-10-22，正文为：'All financial statements exported as PDF. CRM pipeline, HR metrics, and event performance compiled. Overdue collection tasks assigned. Review note: Q3 Operations Review -- Complete -- 2026-09-30.'

**步骤：**

1. 在 BigCapital 中，生成并导出为 PDF：Profit and Loss Sheet、Balance Sheet、A/R Aging Summary、A/P Aging Summary、Cash Flow Statement 和 Sales by Items 报告，全部筛选到季度日期范围 2026-07-01 到 2026-09-30，且记账基础为 Accrual。记录每份报告的关键数字。从 A/R Aging Summary 中推导出唯一的逾期客户名称列表（在 61-90 天或 90+ 天账龄区间中余额非零的客户）——该 OVERDUE_CLIENTS 列表将在后续 Twenty CRM 步骤中使用。
2. 在 Twenty CRM 中，使用筛选栏分别添加 Stage = 'Won' 且 Close Date 在 2026-07-01 到 2026-09-30 之间的条件，然后按相同日期范围筛选 'Lost'，再筛选开放阶段 'SCREENING'，记录每种情况的数量和总额。对于从 BigCapital 推导出的 OVERDUE_CLIENTS 列表中的每一个唯一客户名称，在 Twenty 中搜索匹配公司；如果找到精确匹配，则创建一个关联任务，否则创建一个不关联的任务，且客户名称必须与 A/R Aging 报告中显示的完全一致。创建一个标题为 'Q3 Pipeline Summary -- 2026-09-30' 的管道摘要笔记，胜率保留一位小数（如果没有 Won+Lost 交易则为 'N/A'），并包含推导出的逾期客户名称。
3. 在 Frappe HRMS 中，查看 2026 年 9 月的 Monthly Attendance Sheet（如果可用则应用公司筛选 'TechVista Solutions Pvt. Ltd.'），如果分页则遍历所有页面。查看 Unpaid Expense Claim 报告（遍历所有页面并对 amount 列求和）。查看 Employee Leave Balance Summary（如果可用则应用请假类型筛选 'Sick Leave'，否则扫描报告输出中是否包含 'Sick Leave'；如果该请假类型未出现则记录为 'Not available'）。查看 Employee Advance Summary（遍历所有页面，统计并汇总未支付/未结清预支）。记录出勤人数、高缺勤员工、未支付报销、请假负债以及开放预支。
4. 在 Pretix 中，查看组织者 'broadway-group' 下 'Hamilton' 的 Event Dashboard，记录总订单数和收入，然后查看 Event Orders Overview，基于产品/项目列统计 'Balcony' 和 'Playbill Program' 的收入，并基于状态筛选或状态列统计订单状态（paid、pending、cancelled）。
5. 在 Twenty CRM 中，创建一条名为 'Q3 Operations Review -- Complete -- 2026-09-30' 的综合运营审查笔记，汇总四个应用中的所有数据，并将胜率保留一位小数（或 'N/A'），包括推导出的逾期客户名称；同时创建一个标题为 'Present Q3 operations review to leadership' 的演示任务，到期日为 2026-10-22，正文引用已完成的审查笔记。

**登录凭证：**

- twenty: tim@apple.dev / tim@apple.dev
- bigcapital: admin@bigcapital.local / admin123
- frappe-hrms: Administrator / admin
- pretix: admin@localhost / admin

