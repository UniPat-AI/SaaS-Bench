**任务要求：**

执行一条从职位需求到录用发放的端到端招聘流程，涵盖 Frappe HRMS 中的招聘预算跟踪、BigCapital 中的招聘费用核算，以及 Twenty CRM 中的招聘协调任务管理。

在 Frappe HRMS 中：(1) 为部门 'Human Resources - TVS'、职位 'Business Analyst' 创建一条 Job Requisition，职位数为 1，预期薪酬为 1100000 INR，描述为 'Expanding the HR team to support data-driven workforce planning and organizational development initiatives requiring strong analytical capabilities.'。提交该 requisition。 (2) 创建一条 Job Opening，职位为 'Business Analyst'，部门为 'Human Resources - TVS'，描述为 'We are seeking a detail-oriented Business Analyst to support HR operations, workforce analytics, and process improvement initiatives. Requirements: 3+ years of business analysis experience, proficiency in data analysis tools (Excel, Power BI), experience with HRMS platforms, strong documentation and communication skills.'，职位数为 1。将状态设为 'Open'。 (3) 创建一名 Job Applicant 'Karan Mehta'，邮箱 'karan.mehta@gmail.com'，关联到该 job opening，来源为 'LinkedIn'，状态为 'Open'。 (4) 创建一名 Job Applicant 'Divya Pillai'，邮箱 'divya.pillai@outlook.com'，关联到该 job opening，来源为 'Employee Referral'，状态为 'Open'。 (5) 创建一名 Job Applicant 'Rohit Nambiar'，邮箱 'rohit.nambiar@yahoo.com'，关联到该 job opening，来源为 'Indeed'，状态为 'Open'。 (6) 创建一个名为 'HR Analytical Skills Test' 的 Interview Round。 (7) 创建一个名为 'HR Director Final Round' 的 Interview Round。 (8) 为 'Karan Mehta' 安排一次 Interview，interview round 为 'HR Analytical Skills Test'，日期为 2026-08-05，interviewer 为 'Ananya Reddy'（HR-EMP-00007）。 (9) 为 'Divya Pillai' 安排一次 Interview，interview round 为 'HR Analytical Skills Test'，日期为 2026-08-05，interviewer 为 'Ananya Reddy'（HR-EMP-00007）。 (10) 为 'Rohit Nambiar' 安排一次 Interview，interview round 为 'HR Analytical Skills Test'，日期为 2026-08-05，interviewer 为 'Pooja Malhotra'（HR-EMP-00008）。 (11) 为 'Karan Mehta' 提交 Interview Feedback，针对 'HR Analytical Skills Test'：评分 5/5，结果 'Cleared'。 (12) 为 'Divya Pillai' 提交 Interview Feedback，针对 'HR Analytical Skills Test'：评分 4/5，结果 'Cleared'。 (13) 为 'Rohit Nambiar' 提交 Interview Feedback，针对 'HR Analytical Skills Test'：评分 2/5，结果 'Rejected'。注意：applicant_3 的首轮评分最低，因此被淘汰。 (14) 将 'Rohit Nambiar' 的状态更新为 'Rejected'。 (15) 为 'Karan Mehta' 安排一次 Interview，interview round 为 'HR Director Final Round'，日期为 2026-08-12，interviewer 为 'Rajesh Kumar'（HR-EMP-00001）。 (16) 为 'Divya Pillai' 安排一次 Interview，interview round 为 'HR Director Final Round'，日期为 2026-08-12，interviewer 为 'Rajesh Kumar'（HR-EMP-00001）。 (17) 为 'Karan Mehta' 提交 Interview Feedback，针对 'HR Director Final Round'：评分 5/5，结果 'Cleared'。 (18) 为 'Divya Pillai' 提交 Interview Feedback，针对 'HR Director Final Round'：评分 4/5，结果 'Cleared'。 (19) 将 'Karan Mehta' 的状态更新为 'Accepted'。 (20) 为 'Karan Mehta' 创建一份 Job Offer，职位为 'Business Analyst'，部门为 'Human Resources - TVS'，offer date 为 2026-08-19，条款中包含基础薪资 1050000 INR。 (21) 打开 Recruitment Analytics 报告。验证职位 'Business Analyst' 的 job opening 显示 1 个职位和 3 名申请人。

在 BigCapital 中：(22) 创建一个名为 'HR Recruitment Cost Account' 的账户，类型为 'Expense'，如果尚不存在。 (23) 创建一条日期为 2026-08-07 的费用记录，金额 4800，费用账户为 'HR Recruitment Cost Account'，从 'Petty Cash' 支付，引用号为 'External recruiter fee - Business Analyst - LinkedIn'。发布该费用。 (24) 创建第二条日期为 2026-08-13 的费用记录，金额 650，费用账户为 'HR Recruitment Cost Account'，从 'Petty Cash' 支付，引用号为 'Job board posting fee - Business Analyst'。发布该费用。 (25) 打开 General Ledger 报告，筛选账户 'HR Recruitment Cost Account'，日期范围 2026-08-07 到 2026-08-13（注意：recruiter_fee_date 总是在 job_board_fee_date 之前或同日）。验证出现两笔借方分录：4800 和 650，总计 5450。 (26) 打开 Profit and Loss Sheet，日期范围 2026-08-07 到 2026-08-13。验证 'HR Recruitment Cost Account' 显示 5450。

在 Twenty CRM 中：(27) 创建一个标题为 'Onboard Karan Mehta - Business Analyst' 的任务，到期日为 2026-09-01，正文为：'Offer accepted. Designation: Business Analyst, Department: Human Resources - TVS. Base salary: 1050000 INR. Offer date: 2026-08-19. Onboarding checklist: IT setup, desk allocation, buddy assignment. Recruitment cost: 5450 INR (recruiter 4800 + job board 650).' (28) 创建一个标题为 'Send rejection notifications - Business Analyst recruitment' 的任务，到期日为 2026-08-21，正文为：'Rejected applicants: Rohit Nambiar (rohit.nambiar@yahoo.com) - did not clear HR Analytical Skills Test (rating 2/5). Divya Pillai - cleared both rounds but not selected (final rating 4/5). Draft rejection emails with feedback summary for each candidate.' (29) 创建一条标题为 'Recruitment Summary - Business Analyst - 2026-08-19' 的笔记，正文为：
'POSITION: Business Analyst (Human Resources - TVS), 1 opening(s)
Requisition justification: Expanding the HR team to support data-driven workforce planning and organizational development initiatives requiring strong analytical capabilities.

APPLICANTS:
- Karan Mehta (karan.mehta@gmail.com, source: LinkedIn): Round 1 = 5/5, Round 2 = 5/5 -> ACCEPTED, offer issued 2026-08-19
- Divya Pillai (divya.pillai@outlook.com, source: Employee Referral): Round 1 = 4/5, Round 2 = 4/5 -> Waitlisted
- Rohit Nambiar (rohit.nambiar@yahoo.com, source: Indeed): Round 1 = 2/5 -> REJECTED

COST:
- Recruiter fee: 4800 INR
- Job board fee: 650 INR
- Total: 5450 INR
- Account: HR Recruitment Cost Account'

**步骤：**

1. 在 Frappe HRMS 中，为部门 'Human Resources - TVS' 和职位 'Business Analyst' 创建一条 job requisition，提交后再创建一条信息相同且状态为 'Open' 的 job opening，并登记三名申请人（Karan Mehta、Divya Pillai、Rohit Nambiar），各自来源不同（LinkedIn、Employee Referral、Indeed）。注意：hiring_department 'Human Resources - TVS' 和 hiring_designation 'Business Analyst' 必须从 Frappe HRMS 实例中已存在的 Department 和 Designation 记录里选择。
2. 在 Frappe HRMS 中，创建两个 interview round（'HR Analytical Skills Test' 和 'HR Director Final Round'），为所有三名申请人安排首轮面试，指定面试官（Ananya Reddy 用于申请人 1 和 2，Pooja Malhotra 用于申请人 3），提交反馈，其中 Rohit Nambiar 的评分最低（2/5）且结果为 'Rejected'，然后将 Rohit Nambiar 状态更新为 'Rejected'。注意：面试官必须是 Frappe HRMS 中已存在的员工。
3. 在 Frappe HRMS 中，为 Karan Mehta 和 Divya Pillai 安排第二轮面试，面试官为 Rajesh Kumar（HR-EMP-00001），提交反馈，两人均通过（评分分别为 5 和 4），将 Karan Mehta 更新为 'Accepted'，并为 Karan Mehta 创建一份基础薪资为 1050000 INR 的 job offer。
4. 在 Frappe HRMS 中，打开 Recruitment Analytics 并验证职位 'Business Analyst' 的 job opening 显示 1 个职位和 3 名申请人。
5. 在 BigCapital 中，如有需要创建招聘费用账户 'HR Recruitment Cost Account'，记录日期为 2026-08-07 的 recruiter fee（4800）和日期为 2026-08-13 的 job board fee（650），作为使用 'Petty Cash' 作为付款账户的已发布费用，然后通过 General Ledger（日期范围：2026-08-07 到 2026-08-13）验证两笔借方共计 5450，以及 Profit and Loss 报告（同日期范围）验证 'HR Recruitment Cost Account' 下显示 5450。
6. 在 Twenty CRM 中，创建一条入职任务 'Onboard Karan Mehta - Business Analyst'（到期 2026-09-01），一条拒绝通知任务 'Send rejection notifications - Business Analyst recruitment'（到期 2026-08-21，引用已拒绝的 Rohit Nambiar 和未被录用的 Divya Pillai），以及一条招聘摘要笔记 'Recruitment Summary - Business Analyst - 2026-08-19'，其中包含所有申请人的结果和成本拆分。

**登录凭证：**

- frappe-hrms: Administrator / admin
- bigcapital: admin@bigcapital.local / admin123
- twenty: jony.ive@apple.dev / tim@apple.dev

