**任务要求：**

执行一条跨 Frappe HRMS、BigCapital 和 Twenty CRM 的班次排班与加班管理工作流。

在 Frappe HRMS 中：(1) 创建名为 'Gamma Shift' 的 Shift Type，开始时间 '06:30'，结束时间 '14:30'。启用来自 Employee Checkin 的自动考勤。将提前离岗宽限期设为 8 分钟，迟到入场宽限期设为 8 分钟。 (2) 创建名为 'Sigma Shift' 的 Shift Type，开始时间 '14:30'，结束时间 '22:30'。启用来自 Employee Checkin 的自动考勤。将提前离岗宽限期设为 8 分钟，迟到入场宽限期设为 8 分钟。 (3) 创建名为 'Theta Shift' 的 Shift Type，开始时间 '22:30'，结束时间 '06:30'。启用来自 Employee Checkin 的自动考勤。设置相同的宽限期。 (4) 打开 Shift Type 列表，验证所有三个 shift type 都存在且开始/结束时间正确。 (5) 使用 Shift Assignment Tool，为部门 'Finance & Accounting - TVS' 的员工批量分配 'Gamma Shift'，日期范围为 2026-09-01 到 2026-09-30。 (6) 为以下员工创建 'Sigma Shift' 的单独 Shift Assignments，日期为 2026-09-01 到 2026-09-30：Kavitha Iyer、Arjun Nair、Ananya Reddy（3 名员工）。 (7) 为以下员工创建 'Theta Shift' 的单独 Shift Assignments，日期为 2026-09-01 到 2026-09-30：Mohammed Farooq、Sanjay Krishnan（2 名员工）。 (8) 处理来自员工 'Deepika Joshi'（HR-EMP-00010）的 Shift Request，申请将其从 'Gamma Shift' 调整为 'Sigma Shift'，日期为 2026-09-12。注意：Deepika Joshi 属于部门 'Finance & Accounting - TVS'，并不在 Kavitha Iyer、Arjun Nair、Ananya Reddy 或 Mohammed Farooq、Sanjay Krishnan 这些员工名单中。创建该 shift request 并批准它。 (9) 打开 Shift Assignment 列表并验证：(a) 存在覆盖部门 'Finance & Accounting - TVS' 且日期为 2026-09-01 到 2026-09-30 的 'Gamma Shift' 有效分配；(b) Kavitha Iyer、Arjun Nair、Ananya Reddy 各自存在 'Sigma Shift' 的单独分配；(c) Mohammed Farooq、Sanjay Krishnan 各自存在 'Theta Shift' 的单独分配；(d) 经过批准的 Deepika Joshi Shift Request 可见且状态为 'Approved'。 (10) 创建一个名为 'Night Differential Overtime' 的 Overtime Type，pay rate multiplier 为 1.25。 (11) 为员工 'Suresh Menon'（HR-EMP-00009）创建一张 Overtime Slip，overtime type 为 'Night Differential Overtime'，6 小时，日期为 2026-09-06。提交该 overtime slip。 (12) 为员工 'Rahul Verma'（HR-EMP-00013）创建一张 Overtime Slip，overtime type 为 'Night Differential Overtime'，4 小时，日期为 2026-09-13。提交该 overtime slip。

在 BigCapital 中：(13) 创建一个名为 'Overtime Shift Differential Expense' 的账户，类型为 'Expense'，如果尚不存在。 (14) 验证 'Accounts Payable (A/P)' 作为负债类型账户存在。如果不存在，则创建一个名为 'Accounts Payable (A/P)' 的负债账户。 (15) 加班成本为：OVERTIME_COST_1 = 375.00（即 6 x 50 x 1.25），OVERTIME_COST_2 = 250.00（即 4 x 50 x 1.25），TOTAL_OVERTIME = 625.00（即 375.00 + 250.00）。 (16) 创建并发布一条日期为 2026-09-30 的手工日记账分录：借记 'Overtime Shift Differential Expense' 625.00，贷记 'Accounts Payable (A/P)' 625.00，备注明为 'Overtime accrual -- Suresh Menon (6h) + Rahul Verma (4h) -- rate 50 x 1.25 multiplier'。 (17) 打开 General Ledger 报告，筛选账户 'Overtime Shift Differential Expense'，日期范围 2026-09-30 到 2026-09-30。验证出现一笔 625.00 的借方分录。 (18) 打开 Profit and Loss Sheet，日期范围 2026-09-01 到 2026-09-30。验证 'Overtime Shift Differential Expense' 显示 625.00。

在 Twenty CRM 中：(19) 创建一个标题为 'Review shift schedule compliance -- 2026-09-01 to 2026-09-30' 的任务，到期日为 2026-10-07，正文为：'Shift schedule deployed:
- Gamma Shift (06:30-14:30): Finance & Accounting - TVS department bulk assigned
- Sigma Shift (14:30-22:30): 3 employees assigned
- Theta Shift (22:30-06:30): 2 employees assigned
Shift swap approved: Deepika Joshi from Gamma Shift to Sigma Shift on 2026-09-12.
Review assignment records for compliance.' (20) 创建一个标题为 'Process overtime payments -- 2026-09-30' 的任务，到期日为 2026-10-14，正文为：'Overtime slips submitted:
- Suresh Menon: 6 hours on 2026-09-06 = 375.00 USD
- Rahul Verma: 4 hours on 2026-09-13 = 250.00 USD
Total: 625.00 USD
Journal entry posted 2026-09-30. Include in next payroll run.' (21) 创建一条标题为 'Shift & Overtime Summary -- 2026-09-01 to 2026-09-30' 的笔记，正文为：
'SHIFT CONFIGURATION:
- Gamma Shift: 06:30-14:30, grace 8 min
- Sigma Shift: 14:30-22:30, grace 8 min
- Theta Shift: 22:30-06:30, grace 8 min

ASSIGNMENTS:
- Morning: Finance & Accounting - TVS department (bulk)
- Evening: Kavitha Iyer, Arjun Nair, Ananya Reddy
- Night: Mohammed Farooq, Sanjay Krishnan
- Swap: Deepika Joshi -> Sigma Shift on 2026-09-12

OVERTIME:
- Suresh Menon: 6h @ 50 x 1.25 = 375.00 USD
- Rahul Verma: 4h @ 50 x 1.25 = 250.00 USD
- Total: 625.00 USD
- Accrual: Overtime Shift Differential Expense (debit) / Accounts Payable (A/P) (credit)'

**步骤：**

1. 在 Frappe HRMS 中，创建三个 shift type（早班 'Gamma Shift'、晚班 'Sigma Shift'、夜班 'Theta Shift'），分别设置开始/结束时间（06:30-14:30、14:30-22:30、22:30-06:30），启用自动考勤，并将宽限期均设为 8 分钟。
2. 将 'Gamma Shift' 批量分配给部门 'Finance & Accounting - TVS'，日期为 2026-09-01 到 2026-09-30。为 3 名晚班员工（Kavitha Iyer、Arjun Nair、Ananya Reddy）和 2 名夜班员工（Mohammed Farooq、Sanjay Krishnan）在相同日期范围内创建单独的 shift assignment。处理并批准 Deepika Joshi 的 shift swap 请求（她属于 'Finance & Accounting - TVS'，且不在晚班或夜班员工名单中），将其在 2026-09-12 从早班换到晚班。
3. 验证 Shift Assignment 列表中的所有 shift assignment 以及已批准的 shift request。创建 overtime type 'Night Differential Overtime'，倍数 1.25。创建并提交 Suresh Menon（2026-09-06，6 小时）和 Rahul Verma（2026-09-13，4 小时）的 overtime slip。
4. 在 BigCapital 中，如有需要创建费用账户 'Overtime Shift Differential Expense'。验证 'Accounts Payable (A/P)' 作为负债账户存在，若不存在则创建。发布一条日期为 2026-09-30 的日记账分录，借记 'Overtime Shift Differential Expense' 625.00，贷记 'Accounts Payable (A/P)' 625.00，并使用指定备忘。通过 General Ledger（'Overtime Shift Differential Expense' 的 625.00 借方）和 Profit & Loss 报告（'Overtime Shift Differential Expense' 显示 625.00）验证。
5. 在 Twenty CRM 中，创建任务 'Review shift schedule compliance -- 2026-09-01 to 2026-09-30'，包含班次部署细节，截止日期为 2026-10-07。创建任务 'Process overtime payments -- 2026-09-30'，包含精确金额（375.00、250.00、625.00）的加班成本拆分，截止日期为 2026-10-14。创建笔记 'Shift & Overtime Summary -- 2026-09-01 to 2026-09-30'，包含完整班次配置、分配详情以及精确金额的加班成本拆分。

**登录凭证：**

- frappe-hrms: Administrator / admin
- bigcapital: admin@bigcapital.local / admin123
- twenty: jane.austen@apple.dev / tim@apple.dev

