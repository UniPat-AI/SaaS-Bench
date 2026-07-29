**任务要求：**

作为一名管理员经理，识别文件存储中已被替代的文档，将它们归档，通过消息通知团队，并发送正式变更通知：(1) 在 ownCloud 中，导航到现有文件夹 'doc/healthcare'。找到文件 Kima_w_Medical_Center_Nursing_Position_Description.docx, Inflammation_Protein_Results_Mann_Whitney_U_Test_Ratios_Sensitivity_Specificity.docx, MassHealth_Medicaid_CHIP_Section_1115_Demonstration_Waiver.docx（3 个文件）。对于每个文件，查看其文件详情（大小和修改日期）并记录下来。将每个文件重命名，在其当前名称前加上 'ARCHIVED_'（例如，'Kima_w_Medical_Center_Nursing_Position_Description.docx' 变为 'ARCHIVED_Kima_w_Medical_Center_Nursing_Position_Description.docx'）。在 'doc/healthcare' 下创建一个名为 'Obsolete_Healthcare_2026H1' 的新文件夹。将所有已重命名的文件移动到 'Obsolete_Healthcare_2026H1'。为每个已移动文件添加标签 'obsolete'。在 'doc/healthcare' 中创建一个名为 'ACTIVE_HEALTHCARE_DOCS.txt' 的新文本文件，内容为 'Current Active Healthcare Documents:
- Kima Medical Center Nursing Position Description v3 (doc/healthcare/nursing_position_v3.docx)
- Inflammation Protein Study Final Report 2026 (doc/healthcare/inflammation_protein_final_2026.docx)
- MassHealth 1115 Waiver Renewal 2026 (doc/healthcare/masshealth_1115_renewal_2026.docx)'，列出当前活动文档名称及其位置。 (2) 在 OnlyOffice 中，在 Common Documents 里创建一个名为 'Healthcare_Archive_Register_2026H1' 的新电子表格。在 Sheet1 中，设置第 1 行表头：Original Filename (A1), Archived Filename (B1), Original Size (C1), Last Modified (D1), Archived Date (E1), Replacement Document (F1)。填入 3 行（第 2 到第 4 行）数据，内容来自 ownCloud：原始名称、带有 'ARCHIVED_' 前缀的归档名称、文件大小、修改日期、今天日期 '2026-04-20'，以及替换文档名称 nursing_position_v3.docx, inflammation_protein_final_2026.docx, masshealth_1115_renewal_2026.docx。再添加一行，在 A 列写入 'Total Archived'，并在 B 列使用 COUNTA 公式统计所有归档文件名。将电子表格共享给用户 'amit.singh' 以便编辑。 (3) 在 Mattermost 中，在 Product & Design 团队里，进入现有频道 'bug-triage'。发布消息 'Healthcare Archive Notice: The following superseded healthcare documents have been moved to the archive folder. Originals: Kima_w_Medical_Center_Nursing_Position_Description.docx, Inflammation_Protein_Results_Mann_Whitney_U_Test_Ratios_Sensitivity_Specificity.docx, MassHealth_Medicaid_CHIP_Section_1115_Demonstration_Waiver.docx. Replacements: nursing_position_v3.docx, inflammation_protein_final_2026.docx, masshealth_1115_renewal_2026.docx. Effective 2026-04-20.'，列出所有已归档文件及其替换项。然后使用 /header 将频道头部设置为 'Report and triage bugs. Healthcare document archive audit complete 2026-04-20. See ACTIVE_HEALTHCARE_DOCS.txt for current versions.'。向用户 'admin' 发送一条直接消息，文本为 'Please review and revoke any external sharing links associated with the archived healthcare files in doc/healthcare/Obsolete_Healthcare_2026H1. Appreciated.'，请求移除任何与已归档文件相关的外部共享。 (4) 在 Roundcube 中，创建一个新的发件人身份，显示名称为 'Admin Manager - Healthcare Records'，邮箱为 'admin.healthcare@mail.local'，organization 为 'Corporate Records Office'，并设置一个纯文本签名 'Admin Manager
Healthcare Records Control
Corporate Records Office
admin.healthcare@mail.local'。使用该身份向 carlos.mendez@mail.local, rachel.goldberg@mail.local, tom.andersen@mail.local, amira.hassan@mail.local 撰写一封邮件，主题为 'Formal Notice: Healthcare Document Supersession Effective 2026-04-20'，正文为 'Dear Department Heads,

Please be advised that the following healthcare documents have been formally superseded and archived as of 2026-04-20:

1. Kima_w_Medical_Center_Nursing_Position_Description.docx -> replaced by nursing_position_v3.docx (effective 2026-04-20)
2. Inflammation_Protein_Results_Mann_Whitney_U_Test_Ratios_Sensitivity_Specificity.docx -> replaced by inflammation_protein_final_2026.docx (effective 2026-04-20)
3. MassHealth_Medicaid_CHIP_Section_1115_Demonstration_Waiver.docx -> replaced by masshealth_1115_renewal_2026.docx (effective 2026-04-20)

All archived originals are now stored in doc/healthcare/Obsolete_Healthcare_2026H1 with the ARCHIVED_ prefix. Please direct your teams to use the replacement documents only.

Regards,
Admin Manager'，列出每个已替代文档、其替换项和生效日期。将消息优先级设为 Normal。发送邮件。然后进入 Settings > Folders，创建一个名为 'Healthcare_Archive_Notices' 的新邮件文件夹。进入 Sent，选择已发送邮件，并将其移动到 'Healthcare_Archive_Notices'。

**步骤：**

1. 在 ownCloud 中，找到已被替代的文件，记录其详情，用 'ARCHIVED_' 前缀重命名，将其移动到新的归档文件夹，为每个文件打标签，并创建一个当前版本索引文件
2. 在 OnlyOffice 中，创建一个归档登记表，记录所有已归档文件及其元数据和替换文档，包括 COUNTA 公式，并与 records manager 共享
3. 在 Mattermost 中，在 operations 频道发布一则归档通知，列出变更，更新频道头部，并向 IT admin 发送 DM 请求权限清理
4. 在 Roundcube 中，创建一个新发件人身份，撰写并发送一封正式变更通知邮件给 department heads，创建一个用于归档通知的邮件文件夹，并将已发送邮件移入其中

**登录凭据：**

- owncloud: admin / admin
- onlyoffice: admin@onlyoffice.local / NewAdmin123!
- mattermost: admin / SeedAdmin1pass
- roundcubemail: james.whitfield@mail.local / User123!
