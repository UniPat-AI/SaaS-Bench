**任务要求：**

完成一整套员工费用报销流程，涵盖 HR 费用申请审批、会计费用入账与付款，以及 CRM 文档记录。在 Frappe HRMS 中：(1) 前往 Expense Claim 列表，打开由员工 'Mohammed Farooq'（HR-EMP-00015）提交的待审批费用申请 'HR-EXP-2026-00006'。确认其中恰好有三条明细，总计 ₹10,350.00。明细分别是：'Travel' ₹8,500.00、'Food' ₹1,500.00、'Calls' ₹350.00。(2) 批准该费用申请。(3) 前往 Unpaid Expense Claim 报告，确认 'Mohammed Farooq' 显示未付金额 ₹10,350.00，作为审批成功的中间确认。在 BigCapital 中：(4) 创建一个名为 'Mohammed Farooq Reimbursement' 的供应商，邮箱为 'mohammed.farooq@gmail.com'。(5) 确保 BigCapital 中存在与费用类型对应的三个项目：'Travel'、'Food' 和 'Calls'。如果其中任一项目不存在，则以对应名称创建为新项目。(6) 为供应商 'Mohammed Farooq Reimbursement' 创建一张日期为 2026-03-20 的账单（purchase invoice），包含三条行项目，分别引用上述已创建/确认的项目：'Travel' ₹8,500.00、'Food' ₹1,500.00、'Calls' ₹350.00。账单总额必须等于 ₹10,350.00。批准（open）该账单。(7) 记录一笔日期为 2026-04-05 的 Payment Made，针对该账单金额 ₹10,350.00，来源账户为 'Bank Account'。(8) 前往 A/P Aging Summary 报告，筛选 as-of date 2026-04-05，并确认 'Mohammed Farooq Reimbursement' 显示余额为零（确认账单已全额支付）。在 Twenty CRM 中：(9) 创建一个标题为 'Expense reimbursement processed — Mohammed Farooq' 的任务，截止日期为 2026-04-05，正文为：'Expense claim HR-EXP-2026-00006 approved and paid. Total: ₹10,350.00. Items: Travel (₹8,500.00), Food (₹1,500.00), Calls (₹350.00). Payment made from Bank Account on 2026-04-05.' 将该任务标记为完成。

**步骤：**

1. 在 Frappe HRMS 中，打开员工 'Mohammed Farooq'（HR-EMP-00015）的费用申请 'HR-EXP-2026-00006'，并确认其恰好有三条明细，总计 ₹10,350.00：'Travel'（₹8,500.00）、'Food'（₹1,500.00）、'Calls'（₹350.00）。
2. 批准该费用申请。
3. 前往 Unpaid Expense Claim 报告，并确认 'Mohammed Farooq' 显示未付金额 ₹10,350.00，作为该申请已正确批准的中间验证。
4. 在 BigCapital 中，创建一个名为 'Mohammed Farooq Reimbursement' 的供应商，邮箱为 'mohammed.farooq@gmail.com'。
5. 确保 BigCapital 中存在名为 'Travel'、'Food' 和 'Calls' 的三个项目。如果某个项目不存在，则以相应名称创建为新项目。
6. 创建一张日期为 2026-03-20、供应商为 'Mohammed Farooq Reimbursement' 的账单，包含三条行项目，引用上述项目：'Travel' ₹8,500.00、'Food' ₹1,500.00、'Calls' ₹350.00。账单总额必须等于 ₹10,350.00。批准（open）该账单。
7. 记录一笔金额为 ₹10,350.00 的 Payment Made，针对该账单，来源于 'Bank Account'，日期为 2026-04-05。
8. 查看截至 2026-04-05 的 A/P Aging Summary，并确认 'Mohammed Farooq Reimbursement' 的余额为零。
9. 在 Twenty CRM 中，创建一个标题为 'Expense reimbursement processed — Mohammed Farooq' 的任务，正文包含完整的报销明细，截止日期设为 2026-04-05，并将任务标记为完成。

**登录凭据：**

- frappe-hrms: Administrator / admin
- bigcapital: admin@bigcapital.local / admin123
- twenty: jane.austen@apple.dev / tim@apple.dev

