**任务要求：**

执行一条财务审计准备工作流，涵盖会计报表生成与对账、HR 薪资与员工数据验证，以及 CRM 文档汇编。在 BigCapital 中：(1) 打开 Trial Balance Sheet 报告，筛选日期范围 2026-01-01 到 2026-12-31，记账基础为 'Accrual'。将报告导出为 PDF。记录总借方和总贷方，以确认二者平衡。 (2) 打开 Balance Sheet 报告，筛选截至日期 2026-12-31，记账基础为 'Accrual'。将报告导出为 PDF。记录总资产、总负债和总权益。 (3) 打开 Profit and Loss Sheet 报告，筛选日期范围 2026-01-01 到 2026-12-31，记账基础为 'Accrual'。将报告导出为 PDF。记录总收入、总费用和净利润。 (4) 打开 Cash Flow Statement，筛选日期范围 2026-01-01 到 2026-12-31。将报告导出为 PDF。记录经营活动净现金流。 (5) 打开 Journal Sheet 报告，筛选日期范围 2026-01-01 到 2026-12-31。将报告导出为 PDF。 (6) 打开 General Ledger 报告，筛选账户 'Bank Account'，日期范围 2026-01-01 到 2026-12-31。验证期末余额为 -$215,382.44。将报告导出为 PDF。 (7) 打开 A/R Aging Summary 报告，筛选截至日期 2026-12-31。将报告导出为 PDF。记录总应收账款。 (8) 打开 A/P Aging Summary 报告，筛选截至日期 2026-12-31。将报告导出为 PDF。记录总应付账款。 (9) 打开 Sales Tax Liability Summary 报告，筛选日期范围 2026-01-01 到 2026-12-31。将报告导出为 PDF。 (10) 将 2027-01-01 之前的所有交易锁定，以防止对审计期间进行修改。验证锁定状态显示交易已锁定。在 Frappe HRMS 中：(11) 打开按公司 'TechVista Solutions Pvt. Ltd.' 筛选的 Employee Information 报告。将员工信息导出为 CSV。记录在职员工总数。 (12) 打开按 3 月和公司 'TechVista Solutions Pvt. Ltd.' 筛选的 Salary Register 报告。记录所有员工的总工资收入、总扣款和总实发工资。导出工资表。 (13) 打开按公司 'TechVista Solutions Pvt. Ltd.' 和工资周期 '2026' 筛选的 Income Tax Deductions 报告。记录全年扣缴的所得税总额。 (14) 打开按公司 'TechVista Solutions Pvt. Ltd.' 和工资周期 '2026' 筛选的 Provident Fund Deductions 报告。记录 PF 扣款总额。 (15) 打开按公司 'TechVista Solutions Pvt. Ltd.' 筛选的 Employee Leave Balance Summary 报告。记录所有员工在请假类型 'Sick Leave' 下的总未结余假期。这代表一项潜在负债。 (16) 打开 Employee Advance Summary 报告。验证不存在状态为 'Unpaid' 的员工预支，或未收回余额超过 $750 的预支。记录任何例外。在 Twenty CRM 中：(17) 打开 Opportunities 列表。按阶段 'Won' 且关闭日期在 2026-01-01 到 2026-12-31 之间筛选。按金额降序排序。记录全年 Won 成交的总数量和总收入。 (18) 创建一个标题为 'Audit Preparation Package — FY 2026' 的笔记，正文包含所有已记录的数字值：'Financial Statements Generated (all exported as PDF):
- Trial Balance: Total Debits = [value from step 1], Total Credits = [value from step 1], balanced confirmed
- Balance Sheet as of 2026-12-31: Total Assets = [value from step 2], Total Liabilities = [value from step 2], Total Equity = [value from step 2]
- P&L 2026-01-01 to 2026-12-31: Total Revenue = [value from step 3], Total Expenses = [value from step 3], Net Income = [value from step 3]
- Cash Flow Statement: Net Cash from Operating Activities = [value from step 4]
- Journal Sheet: All entries for the period exported
- General Ledger (Bank Account): Closing balance verified at -$215,382.44
- A/R Aging as of 2026-12-31: Total Outstanding Receivables = [value from step 7]
- A/P Aging as of 2026-12-31: Total Outstanding Payables = [value from step 8]
- Sales Tax Liability: Exported

Transaction Lock: All transactions before 2027-01-01 locked.

HR Verification:
- Active employees: [count from step 11]
- Final payroll (March): Gross Earnings = [value from step 12], Total Deductions = [value from step 12], Net Pay = [value from step 12]
- Income tax deducted (2026): [value from step 13]
- PF deductions (2026): [value from step 14]
- Outstanding leave liability (Sick Leave): [value from step 15]
- Employee advances: [exceptions from step 16 or "No unclaimed advances above $750"]

CRM Revenue:
- Won deals FY 2026: Count = [value from step 17], Total Revenue = [value from step 17]' (19) 创建一个标题为 'Submit audit package to external auditors — FY 2026' 的任务，截止日期为 2027-03-15，正文为：'All financial statements, HR payroll reports, and CRM revenue summaries have been compiled. Transaction lock applied through 2027-01-01. Package ready for external auditor review.'

**步骤：**

1. 在 BigCapital 中，生成并导出为 PDF 的 Trial Balance、Balance Sheet、Profit and Loss、Cash Flow Statement、Journal Sheet、General Ledger（账户 'Bank Account'）、A/R Aging Summary、A/P Aging Summary 和 Sales Tax Liability Summary，全部限定在财年日期范围 2026-01-01 到 2026-12-31。记录每份报表的具体数值（借/贷、资产/负债/权益、收入/费用/净利润、经营现金流、应收、应付）。验证银行账户期末余额为 -$215,382.44。
2. 在 BigCapital 中锁定 2027-01-01 之前的所有交易，并验证锁定状态。
3. 在 Frappe HRMS 中，将公司 'TechVista Solutions Pvt. Ltd.' 的 Employee Information 报告导出为 CSV 并记录在职员工数量，查看 3 月的 Salary Register 并记录工资/扣款/净工资总额，检查工资周期 '2026' 的 Income Tax Deductions 和 Provident Fund Deductions 报告并记录总额，查看请假类型 'Sick Leave' 的假期余额负债，并验证没有未结员工预支超过 $750。
4. 在 Twenty CRM 中，筛选 2026-01-01 到 2026-12-31 财年内的 Won 商机并记录总数量和总收入。创建一条名为 'Audit Preparation Package — FY 2026' 的完整审计准备笔记，包含从 BigCapital、Frappe HRMS 和 CRM 收集到的所有具体数值。创建一个标题为 'Submit audit package to external auditors — FY 2026' 的任务，截止日期为 2027-03-15。

**登录凭证：**

- bigcapital: admin@bigcapital.local / admin123
- frappe-hrms: Administrator / admin
- twenty: phil.schiler@apple.dev / tim@apple.dev

