**任务要求：**

作为一名合规官，监控即将到期的文档，启动续期，替换过时版本，并通知利益相关者：(1) 在 ownCloud 中，导航到 Tags 视图并按标签 'pending' 过滤文件。从筛选结果中，准确识别 5 个文件：IDCC2022_FosterinCollaborationDMP_JAC.pptx, Ionescu_S1.pptx, Li_et_al_ESR1_mutations_paper_updated_SM.pptx, Module5-Repositories_presentation.pptx, Fathallah_Exeter University_November 2023_slides.pptx。然后导航到包含这些文件的文件夹 'ppt'。在 'ppt' 内创建一个名为 'Retired_2025Q3' 的子文件夹。将所有 5 个即将到期的文件移动到 'Retired_2025Q3'。对于每个已移动文件，通过在文件名后追加 '_EXPIRED_2025-09-30' 来重命名它（例如，'document.txt' 变为 'document_EXPIRED_2025-09-30.txt'）。从每个已归档文件中移除标签 'pending'，并添加标签 'archived'。然后在 'ppt' 中创建 5 个新的空文本文件，分别命名为 IDCC2022_FosterinCollaborationDMP_JAC_2025Q4.txt, Ionescu_S1_2025Q4.txt, Li_et_al_ESR1_mutations_paper_updated_SM_2025Q4.txt, Module5-Repositories_presentation_2025Q4.txt, Fathallah_Exeter_University_November_2023_slides_2025Q4.txt。为每个新文件添加标签 'approved'。 (2) 在 OnlyOffice 中，在 Common Documents 里创建一个名为 'Presentation_Renewal_Register_2025Q3' 的新电子表格。在 Sheet1 中，设置第 1 行表头：Document Name (A1), Archived Filename (B1), Replacement Filename (C1)。填入 5 行，分别对应这 5 个文档的原始文件名、重命名后的归档文件名和替换文件名。再添加一行，在 A 列写入 'Documents Renewed'，并在 C 列使用 COUNTA 公式统计所有替换文件名。将电子表格共享给用户 'Jun Chen' 以便编辑，并共享给用户 'Laura Brown' 以便查看。 (3) 在 Mattermost 中，在 Product & Design 团队里，进入现有频道 'UX Research'。发布消息 'Presentation Document Renewal Notice: Effective 2025-10-01, the following presentation files have been retired and replaced with updated versions: (1) IDCC2022_FosterinCollaborationDMP_JAC.pptx -> IDCC2022_FosterinCollaborationDMP_JAC_2025Q4.txt; (2) Ionescu_S1.pptx -> Ionescu_S1_2025Q4.txt; (3) Li_et_al_ESR1_mutations_paper_updated_SM.pptx -> Li_et_al_ESR1_mutations_paper_updated_SM_2025Q4.txt; (4) Module5-Repositories_presentation.pptx -> Module5-Repositories_presentation_2025Q4.txt; (5) Fathallah_Exeter University_November 2023_slides.pptx -> Fathallah_Exeter_University_November_2023_slides_2025Q4.txt.'，列出每个过期文档及其替换项。然后使用 /purpose 将频道目的设置为 'Share UX research insights, coordinate user interviews, discuss findings, and track Q3 2025 presentation renewals.'。向用户 'genesis, ginny, nilda' 发送一个群组私信，文本为 'Hi research team, the presentation files you previously contributed to in /ppt have been retired as of 2025-10-01. Please review the replacement placeholder files in ownCloud and upload the updated content versions.'，通知他们与其文档相关的续期。 (4) 在 Roundcube 中，导航到 Settings > Preferences > Special Folders，并验证 Archive 文件夹已设为 'Archive'；如果没有，则设置它。然后撰写一封电子邮件给 compliance-oversight@regulator.gov，BCC 为 'compliance-internal@mail.local'，主题为 'Q3 2025 Presentation Document Renewal Notification'，正文为 'Dear Regulatory Authority,

This email confirms the renewal of the following presentation documents effective 2025-10-01:

1. IDCC2022_FosterinCollaborationDMP_JAC (replaced by IDCC2022_FosterinCollaborationDMP_JAC_2025Q4)
2. Ionescu_S1 (replaced by Ionescu_S1_2025Q4)
3. Li_et_al_ESR1_mutations_paper_updated_SM (replaced by Li_et_al_ESR1_mutations_paper_updated_SM_2025Q4)
4. Module5-Repositories_presentation (replaced by Module5-Repositories_presentation_2025Q4)
5. Fathallah_Exeter_University_November_2023_slides (replaced by Fathallah_Exeter_University_November_2023_slides_2025Q4)

Prior versions have been retired and archived. Please acknowledge receipt.

Best regards,
Compliance Office'，列出每个已续期文档及其新的生效日期。将消息优先级设为 High。发送邮件。然后在 Sent 文件夹中选择已发送邮件，并使用 archive 功能将其归档。

**步骤：**

1. 在 ownCloud 中，使用 Tags 视图按 'pending' 过滤并识别 5 个文件（IDCC2022_FosterinCollaborationDMP_JAC.pptx, Ionescu_S1.pptx, Li_et_al_ESR1_mutations_paper_updated_SM.pptx, Module5-Repositories_presentation.pptx, Fathallah_Exeter University_November 2023_slides.pptx）。导航到 'ppt'，创建 'Retired_2025Q3'，并将所有即将到期的文件移动进去。将每个文件重命名并加上 '_EXPIRED_2025-09-30' 后缀。移除 'pending' 并为每个文件添加 'archived'。在 'ppt' 中创建 5 个空白替换文本文件，并为每个文件打上 'approved' 标签。
2. 在 OnlyOffice 中，在 Common Documents 里创建电子表格 'Presentation_Renewal_Register_2025Q3'，表头为 Document Name, Archived Filename, Replacement Filename。填入 5 行原始、归档和替换文件名。再添加一行，在 A 列写入 'Documents Renewed'，并在 C 列使用 COUNTA 公式。与 'Jun Chen'（编辑）和 'Laura Brown'（查看）共享。
3. 在 Mattermost 中，在 'UX Research'（Product & Design team）里发布续期通知，列出所有 5 个过期->替换映射。使用 /purpose 将频道目的更新为 'Share UX research insights, coordinate user interviews, discuss findings, and track Q3 2025 presentation renewals.'。向 'genesis, ginny, nilda' 发送群组 DM，内容为 'Hi research team, the presentation files you previously contributed to in /ppt have been retired as of 2025-10-01. Please review the replacement placeholder files in ownCloud and upload the updated content versions.'。
4. 在 Roundcube 中，在 Special Folders 中验证/设置 Archive 文件夹为 'Archive'。撰写并发送一封给 'compliance-oversight@regulator.gov'（BCC 'compliance-internal@mail.local'）的邮件，主题为 'Q3 2025 Presentation Document Renewal Notification'，正文列出每个续期文档，并将优先级设为 High。将 Sent 中的已发送邮件归档。

**登录凭据：**

- owncloud: admin / admin
- onlyoffice: admin@onlyoffice.local / NewAdmin123!
- mattermost: admin / SeedAdmin1pass
- roundcubemail: james.whitfield@mail.local / User123!
