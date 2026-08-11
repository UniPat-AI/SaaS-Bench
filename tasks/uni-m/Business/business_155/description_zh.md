**任务要求：**

执行一条员工申诉处理与解决工作流，涵盖 Frappe HRMS 中的申诉提交与调查、BigCapital 中的法律/顾问费用会计、Pretix 中用于政策宣导的活动培训环节，以及 Twenty CRM 中的保密调查任务管理。

在 Frappe HRMS 中：(1) 打开 Grievance Type 列表。如果不存在，则创建 grievance type 'Workplace Harassment'。如果不存在，则创建第二个 grievance type 'Retaliation'。 (2) 为员工 'Pooja Malhotra'（HR-EMP-00008）创建一条 Employee Grievance，grievance type 为 'Workplace Harassment'，grievance against party type 为 'Employee'，grievance against 为 'Arjun Nair'（HR-EMP-00011），subject 为 'Repeated hostile behavior in team meetings'，description 为 'The grievant has reported a pattern of hostile and intimidating behavior by the respondent during weekly project meetings, creating an unsafe work environment.'。 (3) 打开 Employee Grievance 列表，验证 'Pooja Malhotra' 的 grievance 存在且状态为 'Open'。 (4) 打开员工 'Pooja Malhotra'（HR-EMP-00008）的记录，验证其部门为 'Human Resources - TVS'，职位为 'HR Executive'。 (5) 打开员工 'Arjun Nair'（HR-EMP-00011）的记录，验证其部门为 'Sales & Marketing - TVS'。 (6) 为 'Arjun Nair'（HR-EMP-00011）创建一条 Employee Transfer，transfer date 为 2025-07-01，将部门从 'Sales & Marketing - TVS' 改为 'Customer Service - TVS'。提交该转移。 (7) 打开按部门 'Customer Service - TVS' 筛选的 Employee Information 报告。验证 'Arjun Nair' 出现在该部门中。 (8) 创建一个名为 'Workplace Policy Compliance 2025' 的 Training Program，描述为 'Workplace policy compliance training - triggered by grievance investigation'。 (9) 创建一个名为 'Policy Awareness Workshop - Q3 2025' 的 Training Event，关联到 training program 'Workplace Policy Compliance 2025'，开始日期 2025-07-15，结束日期 2025-07-15，类型为 'Workshop'。恰好添加 3 名员工作为参与者：Pooja Malhotra、Arjun Nair 和 Rajesh Kumar。

在 BigCapital 中：(10) 创建一个名为 'Legal and Advisory Fees' 的账户，类型为 'Expense'，如果尚不存在。 (11) 创建一条日期为 2025-07-05 的费用记录，金额 2800，费用账户为 'Legal and Advisory Fees'，从 'Other Expenses' 支付，引用号为 'External investigation advisory - grievance Repeated hostile behavior in team meetings'。发布该费用。 (12) 创建第二条日期为 2025-07-20 的费用记录，金额 1700，费用账户为 'Legal and Advisory Fees'，从 'Other Expenses' 支付，引用号为 'Mediation services - grievance resolution'。发布该费用。 (13) 打开 General Ledger 报告，筛选账户 'Legal and Advisory Fees'，日期范围 2025-07-05 到 2025-07-20。验证出现两笔分录：2800 和 1700，总计 4500。 (14) 打开 Profit and Loss Sheet，日期范围 2025-07-05 到 2025-07-20。验证 'Legal and Advisory Fees' 显示 4500。

在 Pretix 中：(15) 创建一个新的活动 'Workplace Policy Compliance Workshop'，slug 为 'policy-compliance-workshop'，归属于组织者 'edu-workshop'，开始日期为 2025-07-15，货币为 'USD'。 (16) 创建一个产品 'Policy Training Admission'，价格为 0（内部免费培训）。 (17) 创建一个配额 'Training Capacity'，大小为 50，并关联到 'Policy Training Admission'。 (18) 创建一个类型为 'Text (one line)' 的自定义问题，文本为 'Employee ID'，并将其设为 'Policy Training Admission' 的必填项。 (19) 创建一个类型为 'Choice (single)' 的自定义问题，文本为 'Department'，选项为 'Human Resources'、'Sales & Marketing'、'Customer Service'，并将其设为 'Policy Training Admission' 的必填项。 (20) 创建一个名为 'Training Attendance Check-in' 的签到名单，关联到 'Policy Training Admission'。 (21) 将活动设为公开。

在 Twenty CRM 中：(22) 创建一个标题为 'CONFIDENTIAL: Investigate grievance - Repeated hostile behavior in team meetings' 的任务，到期日为 2025-07-12，正文为：'Grievant: Pooja Malhotra (HR-EMP-00008), dept Human Resources - TVS. Respondent: Arjun Nair (HR-EMP-00011), dept Sales & Marketing - TVS. Type: Workplace Harassment. Description: The grievant has reported a pattern of hostile and intimidating behavior by the respondent during weekly project meetings, creating an unsafe work environment.. Investigation advisory cost: 2800 USD. Deadline for findings: 2025-07-12.' (23) 创建一个标题为 'CONFIDENTIAL: Mediation session - Pooja Malhotra and Arjun Nair' 的任务，到期日为 2025-07-25，正文为：'Schedule mediation between Pooja Malhotra and Arjun Nair. Mediation cost: 1700 USD. Arjun Nair transferred to Customer Service - TVS effective 2025-07-01 as interim measure.' (24) 创建一个标题为 'Mandatory compliance training - Workplace Policy Compliance Workshop' 的任务，到期日为 2025-07-15，正文为：'All-hands policy training on 2025-07-15. 3 employees enrolled in HRMS. Pretix registration live for attendance tracking. Check-in list: Training Attendance Check-in. Ensure 100% attendance.' (25) 创建一条标题为 'Grievance Resolution Log - Repeated hostile behavior in team meetings - 2025-07-12' 的笔记，正文为：
'CASE DETAILS:
Grievant: Pooja Malhotra (HR-EMP-00008) - Human Resources - TVS, HR Executive
Respondent: Arjun Nair (HR-EMP-00011) - originally Sales & Marketing - TVS
Type: Workplace Harassment
Subject: Repeated hostile behavior in team meetings

ACTIONS TAKEN:
1. Grievance filed and recorded in HRMS (status: Open)
2. Arjun Nair transferred to Customer Service - TVS effective 2025-07-01
3. External investigation advisory engaged: 2800 USD on 2025-07-05
4. Mediation services engaged: 1700 USD on 2025-07-20
5. Total legal/advisory cost: 4500 USD (account: Legal and Advisory Fees)
6. Mandatory compliance training scheduled: Workplace Policy Compliance Workshop on 2025-07-15
7. 3 employees enrolled, Pretix check-in configured'

**步骤：**

1. 在 Frappe HRMS 中，创建 grievance types 'Workplace Harassment' 和 'Retaliation'（如不存在），然后为 'Pooja Malhotra' 提交一条员工 grievance，投诉对象为 'Arjun Nair'，并验证 grievance 为 Open 且两位员工的部门/职位信息匹配。
2. 在 Frappe HRMS 中，创建并提交一条将 'Arjun Nair' 从 'Sales & Marketing - TVS' 转移到 'Customer Service - TVS' 的 Employee Transfer，通过 Employee Information 报告验证，然后创建 Training Program 和 Training Event，且恰好有 3 名指定参与者。
3. 在 BigCapital 中，如有需要创建费用账户，记录并发布两条费用分录（调查顾问与调解服务）到 'Legal and Advisory Fees'，然后验证 General Ledger 显示两笔分录以及 Profit and Loss Sheet 显示总额。
4. 在 Pretix 中，创建一个免费的政策培训活动，包含产品、配额、两个自定义问题（Employee ID 文本字段和 Department 单选）、一个签到名单，并将活动设为公开。
5. 在 Twenty CRM 中，创建三条任务（保密调查、保密调解、强制培训）以及一条详细的 grievance resolution log 笔记，总结所有四个应用中的所有操作。

**登录凭证：**

- frappe-hrms: Administrator / admin
- bigcapital: admin@bigcapital.local / admin123
- pretix: admin@localhost / admin
- twenty: jony.ive@apple.dev / tim@apple.dev

