**任务要求：**

作为一名转诊协调员，为患者 Dortha Brakus 整理一份跨多次就诊的临床摘要，并向心脏病专科医生出具正式转诊信。(1) 在 OpenEMR 中，打开 Dortha Brakus 的病历，进入其 Issues 列表，并记录所有活动中的医疗问题。进入其就诊历史，回顾最近 5 次就诊，记录每次的日期、诊断（ICD-10 code）以及任何处方。新增一条过敏记录，项目为 'Iodinated contrast media'，反应为 'Anaphylactoid reaction with hypotension'，严重程度为 'Severe'。然后创建一次新的就诊，在 Clinical Notes 表单中填写转诊原因 'Progressive dyspnea on exertion with reduced ejection fraction on recent echocardiogram, suspected heart failure requiring specialist management'，并通过 Fee Sheet 添加 ICD-10 诊断 'I50.22' — 'Chronic systolic (congestive) heart failure'。(2) 在 OnlyOffice 中，创建一个新文档，标题为 'Cardiology Referral — Dortha Brakus'。信函结构应包含：页眉（诊所名称 'Hingham Senior Care Medical Group'，日期）、患者信息部分（姓名、DOB '1953-11-26'、步骤 1 中记录的活动问题列表）、就诊摘要部分（表格列为：Date, Diagnosis Code, Diagnosis Description, Medications — 使用最近 5 次就诊的数据填写）、过敏部分（列出新添加的过敏）、转诊部分（原因 'Progressive dyspnea on exertion with reduced ejection fraction on recent echocardiogram, suspected heart failure requiring specialist management'，请求方医生 'Dr. Rebecca Lindstrom'，接收专科医生 'Dr. Hiroshi Tanaka, Advanced Heart Failure & Transplant Cardiology'），以及结尾和医生签名行。

**步骤：**

1. 登录 OpenEMR，使用 Patient Finder 搜索并打开 Dortha Brakus 的病历。进入 Issues 页面（Medical Problems, Allergies, Medications）并记录所有活动中的医疗问题（标题及其 ICD-10 代码，如有）。
2. 从患者仪表板查看最近 5 次就诊。对每次就诊，记录就诊日期、列出的任何 ICD-10 诊断代码，以及记录的任何处方或药物。
3. 在 Issues 页面新增一条过敏记录：类型设为 Allergy，标题设为 'Iodinated contrast media'，reaction 设为 'Anaphylactoid reaction with hypotension'，severity 设为 'Severe'，status 设为 active。保存该过敏记录。
4. 为 Dortha Brakus 创建一次新的就诊。打开 Clinical Notes 表单并输入备注：'Referral to cardiology — Progressive dyspnea on exertion with reduced ejection fraction on recent echocardiogram, suspected heart failure requiring specialist management'。保存。打开 Fee Sheet，搜索并添加 ICD-10 code 'I50.22' (Chronic systolic (congestive) heart failure)。保存 Fee Sheet。
5. 登录 OnlyOffice Documents，创建一个标题为 'Cardiology Referral — Dortha Brakus' 的新文档。页眉写入诊所名称 'Hingham Senior Care Medical Group' 和今天的日期。添加患者信息部分，包含姓名 'Dortha Brakus'、DOB '1953-11-26'，以及来自步骤 1 的所有活动医疗问题的项目符号列表。
6. 插入一个表格，列为：Date, Diagnosis Code, Diagnosis Description, Medications。用步骤 2 中回顾的 5 次就诊数据填充。表格下方添加 Allergies 部分，列出 'Iodinated contrast media'，reaction 为 'Anaphylactoid reaction with hypotension'，severity 为 'Severe'。
7. 添加 Referral Details 部分，包括：Reason for Referral 'Progressive dyspnea on exertion with reduced ejection fraction on recent echocardiogram, suspected heart failure requiring specialist management'，Requesting Provider 'Dr. Rebecca Lindstrom'，Receiving Specialist 'Dr. Hiroshi Tanaka, Advanced Heart Failure & Transplant Cardiology'。添加结尾段落和请求方医生签名行。保存文档。

**登录凭据：**

- openemr: admin / pass
- onlyoffice: admin@onlyoffice.local / NewAdmin123!
