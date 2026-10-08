**任务要求：**

执行一整套员工离职与最终结算流程，涵盖 HR 离职、工资结算、会计结算分录以及 CRM 任务重分配。在 Frappe HRMS 中：(1) 前往员工记录 'Ananya Reddy'（HR-EMP-00007），并确认其当前部门为 'Human Resources - TVS'，职位为 'HR Executive'。(2) 为 'Ananya Reddy' 创建一条 Employee Separation 记录，离职日期为 2026-06-30。手动添加三项离职活动：'Return company laptop' 分配给 'Rajesh Kumar'，'Revoke system access' 分配给 'Rajesh Kumar'，以及 'Conduct exit interview' 分配给 'Pooja Malhotra'。提交该离职记录。(3) 前往 Employee Exits 报告，确认 'Ananya Reddy' 以离职日期 2026-06-30 显示。在 BigCapital 中：(6) 首先创建一个名为 'Ananya Reddy - Ex Employee' 的新供应商，显示名称为 'Ananya Reddy'，邮箱为 'ananya.reddy@gmail.com'，以便后续向其付款。(7) 创建一条日期为 2026-06-30 的手动 journal entry，分录如下：借记 'Rent' 57,950.00（final month pro-rata salary），借记 'Advertising Expense' 29,000.00（leave encashment），贷记 'Opening Balance Liabilities' 86,950.00（final salary 和 encashment 之和）。Memo：'Final settlement — Ananya Reddy — 2026-06-30'。发布该 journal entry。(8) 记录一笔日期为 2026-07-05 的 Payment Made，支付给供应商 'Ananya Reddy - Ex Employee' 86,950.00，来源账户为 'Sales of Product Income'。Reference/memo：'Final settlement — Ananya Reddy — 2026-06-30'。(9) 前往 General Ledger 报告，筛选 'Rent'，日期范围为 2026-06-01 至 2026-07-05，并验证存在一条日期为 2026-06-30、借记金额 57,950.00、备注包含 'Final settlement — Ananya Reddy — 2026-06-30' 的借方分录。在 Twenty CRM 中：(10) 前往 Twenty CRM 中的公司记录 'MetricStream'。创建 3 个新任务，全部关联到公司 'MetricStream'，标题必须严格为：['Schedule MetricStream compliance review meeting', 'Update MetricStream primary contact details', 'Follow up on MetricStream contract renewal']。每个任务的截止日期都必须为 2026-07-20，且正文必须完全为：'Reassigned from Ananya Reddy (separated 2026-06-30). Original responsibility transferred — review and update client contacts.' (11) 创建一条标题为 'Employee Separation Complete — Ananya Reddy' 的 note，正文必须完全为：'Separation date: 2026-06-30. Final settlement: 86,950.00 (salary: 57,950.00, leave encashment: 29,000.00). Payment processed 2026-07-05 from Sales of Product Income. 3 client tasks reassigned to company MetricStream.'

**步骤：**

1. 在 Frappe HRMS 中，打开员工 'Ananya Reddy'（HR-EMP-00007）的记录，确认部门为 'Human Resources - TVS'，职位为 'HR Executive'。
2. 在 Frappe HRMS 中，为 'Ananya Reddy' 创建 Employee Separation，离职日期为 2026-06-30。手动添加三项离职活动：'Return company laptop' 分配给 'Rajesh Kumar'、'Revoke system access' 分配给 'Rajesh Kumar'、以及 'Conduct exit interview' 分配给 'Pooja Malhotra'。提交该离职记录。
3. 在 BigCapital 中，创建一个新供应商，名称为 'Ananya Reddy - Ex Employee'，显示名称为 'Ananya Reddy'，邮箱为 'ananya.reddy@gmail.com'。
4. 在 BigCapital 中，创建并发布一条日期为 2026-06-30 的手动 journal entry：借记 'Rent' 57,950.00 和 'Advertising Expense' 29,000.00，贷记 'Opening Balance Liabilities' 86,950.00，备注为 'Final settlement — Ananya Reddy — 2026-06-30'。
5. 在 BigCapital 中，记录一笔日期为 2026-07-05、支付给 'Ananya Reddy - Ex Employee' 的 Payment Made，金额 86,950.00，来源于 'Sales of Product Income'，参考为 'Final settlement — Ananya Reddy — 2026-06-30'。然后在 General Ledger 中针对 'Rent'（日期范围 2026-06-01 至 2026-07-05）验证存在一条日期为 2026-06-30、借记金额 57,950.00、备注匹配 'Final settlement — Ananya Reddy — 2026-06-30' 的记录。
6. 在 Twenty CRM 中，前往公司 'MetricStream'，并创建 3 个关联到该公司的任务，标题分别为 ['Schedule MetricStream compliance review meeting', 'Update MetricStream primary contact details', 'Follow up on MetricStream contract renewal']，每个任务的截止日期为 2026-07-20，正文为：'Reassigned from Ananya Reddy (separated 2026-06-30). Original responsibility transferred — review and update client contacts.'
7. 在 Twenty CRM 中，创建一条标题为 'Employee Separation Complete — Ananya Reddy' 的 note，正文需包含离职日期、结算金额（86,950.00、57,950.00、29,000.00）、付款日期 2026-07-05、银行账户 Sales of Product Income，以及重分配数量 3 和公司 MetricStream。

**登录凭据：**

- frappe-hrms: Administrator / admin
- bigcapital: admin@bigcapital.local / admin123
- twenty: jony.ive@apple.dev / tim@apple.dev

