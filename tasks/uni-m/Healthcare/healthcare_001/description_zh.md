**任务要求：**

作为慢性病项目经理，启动一个糖尿病管理项目：(1) 在 OpnForm 中设计一份带条件逻辑的糖尿病随访问卷——包含空腹血糖、HbA1c 自报、用药依从性（1–10 分量表）等字段，并添加一个仅在依从性低于 5 时才显示的条件字段，用于询问依从性障碍；添加一个预填今天日期的日期字段，以及一个用于当前症状的多选字段。 (2) 在 OpenEMR 中，打开患者 Cyrstal Labadie 和 Julianne Mueller 的病历。对每位患者，创建一条新的就诊记录，记录生命体征（第一位血压 144/89，第二位 156/98；体重分别为 80 和 104，单位为 kg）（在 Vitals 表单中，在输入数值之前先将 Weight 单位设为 kg），完成 Care Plan 表单，将目标设为 'Attain HbA1c below 6.8 and stabilize blood pressure within 6 months'，并将说明设为 'Measure fasting glucose each morning before medication, follow renal-friendly diabetic diet, engage in 25 minutes of light resistance exercise 4 days per week, and complete weekly telehealth nursing check-ins'，再添加一条 SOAP note，Assessment 为 'Type 2 Diabetes Mellitus with stage 1 hypertension, fair glycemic control'，Plan 为 'Continue metformin 1000mg BID, add empagliflozin 10mg daily, initiate lisinopril 10mg daily for BP control, recheck HbA1c and renal function in 3 months, refer to cardiology'。 (3) 在 OnlyOffice 中，创建一个名为 'Diabetes Program Multi-Site Tracker June 2026' 的电子表格，列包括：Patient Name、Encounter Date、Systolic BP、Diastolic BP、Weight (kg)、HbA1c、Adherence Score、Care Plan Goal。使用步骤 2 中输入的数据填充两位患者的行。添加一个柱状图比较两位患者的收缩压值，以及第二个图表显示体重值。

**步骤：**

1. 登录 OpnForm，创建一个新的空白表单，标题为 'Diabetes Self-Management Follow-Up Form'。添加字段：Date（预填今天，必填）、数字字段 'Fasting Blood Glucose (mg/dL)'（必填）、数字字段 'HbA1c Self-Report'（必填）、量表字段 'Medication Adherence'（最小 1，最大 10，必填）、多选字段 'Current Symptoms'，选项为：Fatigue、Polyuria、Blurred Vision、Numbness、None。添加一个文本字段 'Barriers to Adherence'，并设置条件逻辑：仅在 Medication Adherence 值小于 5 时显示。将表单设为公开并保存。
2. 登录 OpenEMR，使用 Patient Finder 搜索患者 Cyrstal Labadie。打开其病历并创建一条新就诊记录。在就诊中，打开 Vitals 表单并输入 BP 144/89、weight 80（在 Vitals 表单中，在输入数值之前先将 Weight 单位设为 kg）。打开 Care Plan 表单：将目标设为 'Attain HbA1c below 6.8 and stabilize blood pressure within 6 months'，说明设为 'Measure fasting glucose each morning before medication, follow renal-friendly diabetic diet, engage in 25 minutes of light resistance exercise 4 days per week, and complete weekly telehealth nursing check-ins'。打开 SOAP Notes 表单：输入 Assessment 'Type 2 Diabetes Mellitus with stage 1 hypertension, fair glycemic control' 和 Plan 'Continue metformin 1000mg BID, add empagliflozin 10mg daily, initiate lisinopril 10mg daily for BP control, recheck HbA1c and renal function in 3 months, refer to cardiology'。保存所有表单。
3. 为患者 Julianne Mueller 重复就诊创建过程：创建就诊、记录生命体征（BP 156/98，weight 104）（在 Vitals 表单中，在输入数值之前先将 Weight 单位设为 kg）、添加相同目标和说明的 Care Plan，以及具有相同 Assessment 和 Plan 的 SOAP note。保存所有表单。
4. 登录 OnlyOffice Documents，创建一个新的电子表格，标题为 'Diabetes Program Multi-Site Tracker June 2026'。在 A1 输入 'Patient Name'，B1 输入 'Encounter Date'，C1 输入 'Systolic BP'，D1 输入 'Diastolic BP'，E1 输入 'Weight (kg)'，F1 输入 'HbA1c'，G1 输入 'Adherence Score'，H1 输入 'Care Plan Goal'。在第 2 行，使用步骤 2 中的值输入 Cyrstal Labadie 的数据。在第 3 行，使用步骤 3 中的值输入 Julianne Mueller 的数据。对于 HbA1c 和 Adherence Score 列，输入占位值 '6.8'、'8.7'、'9'、'4'。
5. 在同一电子表格中，选择两位患者的 Systolic BP 数据并插入一个柱状图比较其数值。然后选择 Weight 数据并插入第二个柱状图比较体重值。保存电子表格。

**登录凭证：**

- opnform: seeded_admin@example.com / mw-admin-123
- openemr: admin / pass
- onlyoffice: admin@onlyoffice.local / NewAdmin123!

