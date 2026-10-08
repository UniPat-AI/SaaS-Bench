**任务要求：**

执行一条供应商付款与费用对账工作流，涵盖会计账单管理、银行规则配置、CRM 供应商跟踪以及 HR 费用验证。在 BigCapital 中：(1) 创建供应商 'Vantage Systems LLC'，邮箱为 'ap@vantagesystems.com'，期初余额为 800。 (2) 创建供应商 'Luminary Consulting Group'，邮箱为 'billing@luminarycg.com'，期初余额为 350。 (3) 创建项目 'ERP Implementation Services'，类型为 Service，成本价为 280，描述为 'Full-cycle ERP deployment, configuration, and user training'。 (4) 创建项目 'Business Process Optimization'，类型为 Service，成本价为 160，描述为 'Workflow analysis and process improvement consulting services'。 (5) 为供应商 'Vantage Systems LLC' 创建一张日期为 2025-10-06 的账单，包含两条明细：'ERP Implementation Services' 数量 3，单价 280，以及 'Business Process Optimization' 数量 2，单价 160。账单总额应为 1160（即 3 × 280 + 2 × 160）。打开该账单。 (6) 为供应商 'Luminary Consulting Group' 创建第二张日期为 2025-10-15 的账单，包含一条明细：'Business Process Optimization' 数量 5，单价 160。账单总额应为 800（即 5 × 160）。打开该账单。 (7) 记录一笔日期为 2025-10-21 的 Payment Made，针对供应商 'Vantage Systems LLC' 的第一张账单，从账户 'Bank Account' 支付 950。 (8) 记录一笔日期为 2025-10-27 的 Payment Made，针对供应商 'Luminary Consulting Group' 的第二张账单，从账户 'Bank Account' 支付 650。 (9) 打开 Vendors Balance Summary 报告，验证 'Vantage Systems LLC' 的余额为 1010，'Luminary Consulting Group' 的余额为 500。注：供应商剩余余额 = 期初余额 + 账单总额 − 付款金额，因此 1010 = 800 + 1160 − 950，500 = 350 + 800 − 650。 (10) 打开 Transactions by Vendors 报告，筛选日期范围 2025-10-06 到 2025-10-27。验证供应商 'Vantage Systems LLC' 恰好显示 2 笔交易：一张 1160 的账单和一笔 950 的付款；供应商 'Luminary Consulting Group' 恰好显示 2 笔交易：一张 800 的账单和一笔 650 的付款。 (11) 打开 Purchases by Items 报告，筛选日期范围 2025-10-06 到 2025-10-15。验证 'ERP Implementation Services' 的采购总额为 840（即 3 × 280），以及 'Business Process Optimization' 的采购总额为 1120（即 2 × 160 + 5 × 160）。 (12) 创建一条日期为 2025-10-29 的费用记录，金额 210，费用科目为 'Cost of Goods Sold'，从 'Petty Cash' 支付，引用号为 'MISC-EXP-2025-004'。发布该费用。 (13) 打开 General Ledger 报告，筛选账户 'Bank Account'，日期范围 2025-10-21 到 2025-10-27。验证出现 950 和 650 的贷方分录。 (14) 创建一个名为 'Cost of Goods Auto-Categorization' 的银行规则，匹配描述包含 'cogs' 的交易，并将其归类到账户 'Cost of Goods Sold'。在 Twenty CRM 中：(15) 创建公司 'Vantage Systems LLC'，域名为 'vantagesystems.com'。 (16) 创建人员 'Harrison Blake'，邮箱为 'ap@vantagesystems.com'，职位为 'Vendor Relations Manager'，并关联到公司 'Vantage Systems LLC'。 (17) 创建公司 'Luminary Consulting Group'，域名为 'luminarycg.com'。 (18) 创建人员 'Celeste Moreau'，邮箱为 'billing@luminarycg.com'，职位为 'Senior Consultant'，并关联到公司 'Luminary Consulting Group'。 (19) 创建一个标题为 'Vendor Payment Reconciliation — 2025-10-27' 的笔记，正文为：'Vendor 1: Vantage Systems LLC — Bill 2025-10-06 total 1160, paid 950 on 2025-10-21, remaining 1010.
Vendor 2: Luminary Consulting Group — Bill 2025-10-15 total 800, paid 650 on 2025-10-27, remaining 500.
Misc expense: 210 from Petty Cash on 2025-10-29.
Bank rule created: Cost of Goods Auto-Categorization.
Service item 1 purchase total: 840.
Service item 2 purchase total: 1120.' (20) 将公司 'Vantage Systems LLC' 添加到收藏夹。在 Frappe HRMS 中：(21) 打开 Expense Claim Type 列表。验证费用报销类型 'Calls' 存在，且其描述设置为 'Linked to BigCapital account: Cost of Goods Sold'。如果不存在，则创建它并将描述设为 'Linked to BigCapital account: Cost of Goods Sold'。对 'Food' 重复同样操作，描述为 'Linked to BigCapital account: Advertising Expense'。 (22) 为员工 'Deepika Joshi' 创建一张新的 Expense Claim，过账日期为 2025-10-29，费用类型为 'Calls'，金额为 210，描述为 'Cross-reference: petty cash disbursement MISC-EXP-2025-004 from Petty Cash'。提交该费用报销。

**步骤：**

1. 在 BigCapital 中，创建两个带期初余额的供应商、两个服务项目以及两张账单（每个供应商一张），并填写相应的明细。打开两张账单。
2. 针对两张账单分别从银行账户记录付款。打开 Vendors Balance Summary 报告并验证剩余余额（期初余额 + 账单总额 − 付款金额）。打开 Transactions by Vendors 报告并筛选 2025-10-06–2025-10-27，验证每个供应商恰好显示 2 笔交易（一张账单、一笔付款）且金额正确。打开 Purchases by Items 报告并筛选 2025-10-06–2025-10-15，验证 'ERP Implementation Services' 显示总额 840，'Business Process Optimization' 显示总额 1120。
3. 创建一条杂项费用并发布。创建银行规则 'Cost of Goods Auto-Categorization'，用于将未来描述包含 'cogs' 的交易自动归类到 'Cost of Goods Sold'。验证 Bank Account 的 General Ledger 显示两笔付款金额对应的贷方分录。
4. 在 Twenty CRM 中，为两个供应商创建公司和联系人记录。创建一条对账摘要笔记，包含所有账单总额、付款金额、剩余余额、杂项费用、银行规则以及按项目的采购总额。将供应商 1 的公司加入收藏夹。
5. 在 Frappe HRMS 中，验证或创建费用报销类型 'Calls' 和 'Food'，确保它们的描述分别匹配 'Linked to BigCapital account: Cost of Goods Sold' 和 'Linked to BigCapital account: Advertising Expense'。创建并提交一张针对员工 'Deepika Joshi' 的 Expense Claim，引用该 Petty Cash 支出。

**登录凭证：**

- bigcapital: admin@bigcapital.local / admin123
- twenty: jane.austen@apple.dev / tim@apple.dev
- frappe-hrms: Administrator / admin

