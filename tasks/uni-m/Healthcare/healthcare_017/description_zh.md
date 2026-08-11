**任务要求：**

作为一名健康信息技术经理，执行重复患者记录识别、合并文档和系统审计工作流：(1) 在 OpenEMR 中，进入 Manage Duplicate Patients 页面并扫描潜在的重复患者记录。识别重复对：Latoyia Kertzmann (pid 158) 和 Latoyia Kertzmann (pid 249)，依据为匹配的人口学信息。合并前，打开两份患者病历，并分别记录以下内容：活动中的医疗问题（来自 Issues）、活动中的药物（来自 Issues）以及任何过敏条目。进入 Merge Patients 页面，选择 Latoyia Kertzmann (pid 249) 作为 source，Latoyia Kertzmann (pid 158) 作为 target，并执行合并。合并后，打开合并后的患者记录（Latoyia Kertzmann (pid 158)），并验证合并后的 Issues 列表包含两份记录中的所有问题、药物和过敏。进入 System Logs，按今天的日期和包含 'merge' 的事件类型进行筛选，记录日志条目的时间戳和用户。然后进入 Address Book，新增一个联系人 'Dr. Yusuf Abdelrahman'，specialty 为 'Gastroenterology'，phone 为 '339-555-0617'，address 为 '1153 Centre Street, Suite 210, Jamaica Plain, MA 02130'。(2) 在 OnlyOffice 中，创建一个标题为 'Kertzmann Duplicate Patient Merge Audit Report - 2026-03-22' 的文档，结构为正式的 Patient Record Merge Audit Report：页眉包含诊所名称 'Metro West Regional Medical Clinic' 和日期；Merge Summary section 包含 source record (Latoyia Kertzmann (pid 249))、target record (Latoyia Kertzmann (pid 158))、merge date 以及 authorizing user 'Administrator'；Pre-Merge Data Comparison section 包含一个表格，列为 Data Element, Source Record Value, Target Record Value, Post-Merge Value — 为两份记录中的每个医疗问题、药物和过敏填充行；System Log Verification section 引用审计日志时间戳并确认日志完整性；Address Book Update section 记录新增的专科联系人；以及 Compliance Certification section，文本为 'I hereby certify that this duplicate patient record merge has been executed in full compliance with HIPAA data integrity requirements under 45 CFR §164.312(c), the ONC Health IT Certification Program record-keeping standards, and the clinic's Health Information Management policy HIM-022; all source-record clinical data has been preserved in the secure audit log for the mandated retention period and is available for regulatory inspection.'，并附上 'Administrator' 的签名块。(3) 在 OnlyOffice 中，再创建一个电子表格，标题为 'Duplicate Patient Record Merge Audit Tracker - March 2026'，列为：Merge ID, Source Patient, Target Patient, Merge Date, Data Elements Transferred, Verified By, Audit Log Confirmed (Yes/No), Notes。为已完成的合并填充一行。添加第二个工作表 'Outside Specialist Contact Registry'，列为：Specialist Name, Specialty, Phone, Address, Date Added — 使用步骤 1 中新增的联系人填充。

**步骤：**

1. 在 OpenEMR 中，进入 Manage Duplicate Patients 并扫描重复项；识别 Latoyia Kertzmann (pid 158) 和 Latoyia Kertzmann (pid 249) 这一对。
2. 打开两份患者病历，并从每位患者的 Issues 列表中记录活动中的医疗问题、药物和过敏。
3. 进入 Merge Patients，将 Latoyia Kertzmann (pid 249) 设为 source、Latoyia Kertzmann (pid 158) 设为 target，并执行合并。
4. 打开 Latoyia Kertzmann (pid 158) 的合并后记录，并验证合并后的 Issues 列表包含两份记录中的所有数据。
5. 进入 System Logs，按今天的日期筛选 merge 事件，并记录日志条目的时间戳和用户。
6. 进入 Address Book，新增专科医生 'Dr. Yusuf Abdelrahman'，并填写 specialty、phone 和 address。
7. 在 OnlyOffice 中创建标题为 'Kertzmann Duplicate Patient Merge Audit Report - 2026-03-22' 的文档，包含合并摘要、合并前数据对照表、系统日志验证、地址簿更新和合规认证部分。
8. 在 OnlyOffice 中创建标题为 'Duplicate Patient Record Merge Audit Tracker - March 2026' 的电子表格，包含一个合并追踪工作表和一个联系人工作表，填入合并数据和专科联系人条目。

**登录凭据：**

- openemr: admin / pass
- onlyoffice: admin@onlyoffice.local / NewAdmin123!
