**任务要求：**

执行一条端到端绩效评估周期，涵盖 Frappe HRMS 中的目标设定、考核评分与奖金处理，BigCapital 中的奖金应计会计，以及 Twenty 中的 CRM 文档。

在 Frappe HRMS 中：(1) 创建一个名为 'Technical Delivery Excellence' 的 KRA。创建第二个名为 'Customer Satisfaction & Retention' 的 KRA。 (2) 创建一个名为 'Engineering Performance Template 2025' 的 Appraisal Template，包含两个 KRA：'Technical Delivery Excellence' 权重 60%，'Customer Satisfaction & Retention' 权重 40%。 (3) 为员工 'Vikram Singh' (HR-EMP-00005) 创建一个目标，标题为 'Reduce sprint defect rate by 30%'，关联到 KRA 'Technical Delivery Excellence'，目标为 '30% reduction in defects per sprint'。再创建第二个目标，标题为 'Improve client NPS score'，关联到 KRA 'Customer Satisfaction & Retention'，目标为 'NPS score >= 75'。 (4) 为员工 'Ananya Reddy' (HR-EMP-00007) 创建一个目标，标题为 'Deliver all project milestones on time'，关联到 KRA 'Technical Delivery Excellence'，目标为 '100% on-time milestone delivery'。再创建第二个目标，标题为 'Achieve zero escalations'，关联到 KRA 'Customer Satisfaction & Retention'，目标为 'Zero client escalations in H2 2025'。 (5) 创建一个名为 'H2 2025 Engineering Performance Review' 的 Appraisal Cycle，开始日期为 2025-07-01，结束日期为 2025-12-31，使用 appraisal template 'Engineering Performance Template 2025'。 (6) 为 'Vikram Singh' (HR-EMP-00005) 创建一条 Appraisal，并关联到 appraisal cycle 'H2 2025 Engineering Performance Review'。将 'Technical Delivery Excellence' 评分为 5/5，'Customer Satisfaction & Retention' 评分为 4/5。提交该 appraisal。将整体得分记录为 EMP1_SCORE（计算方式：(5 x 60 + 4 x 40) / 100 = 4.6）。 (7) 为 'Ananya Reddy' (HR-EMP-00007) 创建一条 Appraisal，并关联到 appraisal cycle 'H2 2025 Engineering Performance Review'。将 'Technical Delivery Excellence' 评分为 3/5，'Customer Satisfaction & Retention' 评分为 4/5。提交该 appraisal。将整体得分记录为 EMP2_SCORE（同样计算 = 3.4）。 (8) 打开 Appraisal Overview 报告。验证 'Vikram Singh' 和 'Ananya Reddy' 都以其在 cycle 'H2 2025 Engineering Performance Review' 中对应的得分出现。 (9) 按如下规则确定奖金金额：如果整体得分 >= 4.0，则 bonus = 15000；如果整体得分 >= 3.0 且 < 4.0，则 bonus = 7500；否则 bonus = 0。直接将未四舍五入的加权整体得分与阈值比较。计算 EMP1_BONUS = 15000（score 4.6 >= 4.0）以及 EMP2_BONUS = 7500（score 3.4 >= 3.0 且 < 4.0）。 (10) 确保 Frappe HRMS 中存在名为 'Performance Bonus' 的工资项目。如果不存在，则创建为适用于 Employee Incentive 工资处理的 'Earning' 类型项目后再继续。 (11) 对于每位符合非零奖金条件的员工，创建一条 Employee Incentive，金额为计算出的奖金，Salary Component 为 'Performance Bonus'，Payroll Date 为 2026-01-31。提交每条 incentive。 (12) 打开 Employee Incentive 列表。验证符合条件的员工存在 incentive 记录，金额正确，状态为 'Submitted'。

在 BigCapital 中：(13) 创建一个名为 'Performance Bonus Expense' 的账户，类型为 'Expense'，如果尚不存在。 (14) 创建一个名为 'Accrued Performance Bonus Payable' 的账户，类型为 'Other Current Liability'，如果尚不存在。 (15) 计算 TOTAL_BONUS = 15000 + 7500 = 22500。创建并发布一条日期为 2025-12-31 的手工日记账分录：借记 'Performance Bonus Expense' 22500，贷记 'Accrued Performance Bonus Payable' 22500，备注明为 'Performance bonus accrual -- H2 2025 Engineering Performance Review -- Vikram Singh (15000), Ananya Reddy (7500)'。 (16) 打开 General Ledger 报告，筛选账户 'Performance Bonus Expense'，日期范围 2025-12-31 到 2025-12-31。验证出现一笔 22500 的借方分录。 (17) 打开 Trial Balance Sheet，筛选日期范围 2025-07-01 到 2025-12-31。验证 'Performance Bonus Expense' 显示 22500 的借方余额，且 'Accrued Performance Bonus Payable' 显示 22500 的贷方余额。

在 Twenty CRM 中：(18) 创建一条标题为 'Appraisal Cycle Results -- H2 2025 Engineering Performance Review' 的笔记，正文为：
'Cycle: H2 2025 Engineering Performance Review (2025-07-01 to 2025-12-31)
Template: Engineering Performance Template 2025
KRAs: Technical Delivery Excellence (60%), Customer Satisfaction & Retention (40%)

Results:
- Vikram Singh (HR-EMP-00005): Technical Delivery Excellence = 5/5, Customer Satisfaction & Retention = 4/5, Overall = 4.6, Bonus = 15000 INR
- Ananya Reddy (HR-EMP-00007): Technical Delivery Excellence = 3/5, Customer Satisfaction & Retention = 4/5, Overall = 3.4, Bonus = 7500 INR

Total bonus liability: 22500 INR
Accrual posted 2025-12-31 -- debit Performance Bonus Expense, credit Accrued Performance Bonus Payable
Bonus payout date: 2026-01-31'
(19) 创建一个标题为 'Process bonus payroll -- H2 2025 Engineering Performance Review' 的任务，截止日期为 2026-01-31，正文为：'Performance bonuses from H2 2025 Engineering Performance Review ready for payroll processing. Total: 22500 INR. Employee incentives submitted in HRMS. Accounting accrual posted. Verify inclusion in next payroll run.'
(20) 创建一个标题为 'Communicate appraisal results to employees' 的任务，截止日期为 2026-01-15，正文为：'Schedule individual meetings to communicate appraisal scores and bonus decisions for H2 2025 Engineering Performance Review. Vikram Singh: score 4.6, bonus 15000. Ananya Reddy: score 3.4, bonus 7500.'

**步骤：**

1. 在 Frappe HRMS 中，创建 KRA 'Technical Delivery Excellence' 和 'Customer Satisfaction & Retention'，然后创建 Appraisal Template 'Engineering Performance Template 2025'，KRA 权重分别为 60% 和 40%。为 Vikram Singh (HR-EMP-00005) 和 Ananya Reddy (HR-EMP-00007) 创建与相应 KRA 关联的目标。
2. 创建 Appraisal Cycle 'H2 2025 Engineering Performance Review'（2025-07-01 到 2025-12-31）。为两名员工创建并提交 appraisal，KRA 评分为（Vikram：5 和 4；Ananya：3 和 4）。验证二者都出现在 Appraisal Overview 报告中，得分分别为 4.6 和 3.4。
3. 应用奖金规则：Vikram（score 4.6 >= 4.0）获得 15000 INR；Ananya（score 3.4 >= 3.0 且 < 4.0）获得 7500 INR。确保工资项目 'Performance Bonus' 存在（如需则创建为 Earning 类型）。为两位员工创建并提交 Employee Incentive 记录。验证 Employee Incentive 列表。
4. 在 BigCapital 中，创建费用账户 'Performance Bonus Expense' 和负债账户 'Accrued Performance Bonus Payable'。发布一条日期为 2025-12-31 的手工日记账分录，借记 'Performance Bonus Expense'、贷记 'Accrued Performance Bonus Payable' 22500。通过 General Ledger 和 Trial Balance 验证。
5. 在 Twenty CRM 中，创建一条笔记 'Appraisal Cycle Results -- H2 2025 Engineering Performance Review'，记录所有得分、奖金和会计细节。创建任务 'Process bonus payroll -- H2 2025 Engineering Performance Review'（到期 2026-01-31）和 'Communicate appraisal results to employees'（到期 2026-01-15）。

**登录凭证：**

- frappe-hrms: Administrator / admin
- bigcapital: admin@bigcapital.local / admin123
- twenty: tim@apple.dev / tim@apple.dev

