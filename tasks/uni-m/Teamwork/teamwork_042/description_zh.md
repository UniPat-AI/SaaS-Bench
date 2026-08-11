**任务要求：**

作为一家创业公司的联合创始人，通过准备合作文件、配置工作区访问并交付入职材料来接纳一名新的自由职业承包人：(1) 在 ownCloud 中，导航到 Users Management 并创建一个新用户账号，用户名为 'diego.martinez'，密码为 'Onboard#2026$'，邮箱为 'diego.martinez@contractors.local'。将该用户加入 'admin' 组。将该用户的存储配额设置为 '10 GB'。然后导航到 All Files 并创建一个名为 'Diego_Martinez_Workspace' 的文件夹。在其中创建子文件夹 'Deliverables' 和 'Briefs'。在 'Briefs' 中创建一个名为 'Mobile_App_Brief.txt' 的文本文件，内容为 'Project: Mobile App MVP. Goal: Ship a cross-platform iOS/Android MVP within 12 weeks using React Native. Primary deliverables include architecture document, authentication module, core feature screens, and production-ready builds for both stores. Review cadence: bi-weekly on Tuesdays.'。为 'Briefs' 创建一个只读权限的公共分享链接，并将过期日期设置为 '2026-09-30'。将 'Diego_Martinez_Workspace' 以读写权限共享给用户 'diego.martinez'。 (2) 在 OnlyOffice 中，在 My Documents 里创建一个新文档，标题为 'Engagement_Letter_Diego_Martinez'。将其结构设置为标题 'Independent Contractor Engagement Agreement'，一个 'Parties' 部分，文本为 'This agreement is entered between Acme Ventures Inc. (the Company) and Diego Martinez (the Contractor), effective May 4, 2026.'，一个 'Scope of Work' 部分，文本为 'Contractor shall provide mobile application development services for the Mobile App MVP initiative, including architecture design, authentication module implementation, core feature development, and App Store/Play Store build preparation, per the attached Project Brief.'，一个 'Compensation' 部分，包含一个有 3 列（Deliverable, Rate, Payment Terms）和 5 行的表格，填入 [["Architecture Document", "$3,000", "Net 15 upon acceptance"], ["Authentication Module", "$5,000", "Net 15 upon acceptance"], ["Core Feature Screens", "$8,000", "50% upfront, 50% on delivery"], ["Store Build & Submission", "$3,500", "Net 15 upon submission"], ["Post-Launch QA & Handoff", "$2,000", "Net 15 upon final sign-off"]]，一个 'Confidentiality' 部分，文本为 'Contractor agrees to maintain strict confidentiality of all proprietary information, source code, user data, and business processes disclosed during the engagement, for a period of 5 years beyond termination.'，以及一个 'Term' 部分，文本为 'This engagement commences on May 4, 2026 and terminates on September 30, 2026, unless extended in writing by both parties.'。将该文档共享给用户 'jun.chen' 以便编辑。将文档标记为收藏。 (3) 在 Mattermost 中，在 Marketing & Growth 团队里，进入现有频道 'Brand Design'。使用 /invite 命令邀请用户 'karrie' 到该频道。发布消息 'Team, please welcome Diego Martinez, our new freelance mobile developer joining us for the Mobile App MVP engagement through September 30. Diego will be partnering with brand and marketing on in-app visual assets and launch collateral — please loop him in on relevant brand reviews.'，介绍该承包人。然后向 'karrie' 发送一条直接消息，文本为 'Hi Diego! Welcome aboard. Your ownCloud workspace is Diego_Martinez_Workspace (read-write). Project brief is here (read-only, expires 2026-09-30): https://owncloud.local/s/briefs-diego-2026. Engagement letter is in OnlyOffice My Documents as Engagement_Letter_Diego_Martinez. Ping me on Mattermost anytime.'，包含工作区访问细节和第 1 步中的公共链接。然后再次进入 'Brand Design' 并使用 /header 将频道头部设置为 'Brand assets, style guides, and design feedback. Contractor onboarding active through 2026-09-30 — welcome Diego Martinez (Mobile App MVP).'. (4) 在 Roundcube 中，导航到 Settings > Preferences > Composing Messages，并验证 HTML editor usage 偏好已设置；如果没有，则将其设置为 'on reply to HTML message'。然后导航到 Settings > Identities，并创建一个新身份，显示名称为 'Sarah O'Brien — Co-Founder'，邮箱为 'sarah.obrien@mail.local'，organization 为 'Acme Ventures Inc.'，并设置一个 HTML 签名 '<p><strong>Sarah O'Brien</strong><br/>Co-Founder, Acme Ventures Inc.<br/><a href="mailto:sarah.obrien@mail.local">sarah.obrien@mail.local</a></p>'。使用该身份撰写一封电子邮件给 'diego.martinez@contractors.local'，CC 为 'ops@acmeventures.local'，主题为 'Welcome to Acme Ventures — Mobile App MVP Onboarding'，正文为 'Hi Diego,

Welcome to the team! Below is a summary of your engagement and workspace access.

Engagement Summary:
- Term: May 4, 2026 – September 30, 2026
- Scope: Mobile App MVP (architecture, auth module, core screens, store builds, QA)
- Compensation: Per the Engagement Letter in OnlyOffice (Engagement_Letter_Diego_Martinez)

Workspace Access:
- ownCloud: Diego_Martinez_Workspace (read-write)
- Project Brief public link (read-only, expires 2026-09-30): https://owncloud.local/s/briefs-diego-2026
- Mattermost: Marketing & Growth → #brand-design

Please review and countersign the engagement letter at your earliest convenience.

Best,
Sarah'，包含合作条款摘要和工作区访问说明。请求发送状态通知（DSN）。发送邮件。

**步骤：**

1. 在 ownCloud Users Management 中，创建一个新用户 'diego.martinez'，设置密码、邮箱、组 'admin' 和配额 '10 GB'。然后在 All Files 中创建 'Diego_Martinez_Workspace'，其下有子文件夹 'Deliverables' 和 'Briefs'。在 briefs 中创建 'Mobile_App_Brief.txt'。为 'Briefs' 创建公共链接（只读，过期时间 '2026-09-30'）。将工作区以读写权限共享给承包人。
2. 在 OnlyOffice 中，在 My Documents 里创建 'Engagement_Letter_Diego_Martinez'，包含 Parties、Scope of Work、Compensation 表格（5 行）、Confidentiality 和 Term 部分。与 'jun.chen' 共享以便编辑。标记为收藏。
3. 在 Mattermost 中，在 'Brand Design' 里使用 /invite 添加 'karrie'。发布 'Team, please welcome Diego Martinez, our new freelance mobile developer joining us for the Mobile App MVP engagement through September 30. Diego will be partnering with brand and marketing on in-app visual assets and launch collateral — please loop him in on relevant brand reviews.'。向 'karrie' 发送包含公共链接的 DM，内容为 'Hi Diego! Welcome aboard. Your ownCloud workspace is Diego_Martinez_Workspace (read-write). Project brief is here (read-only, expires 2026-09-30): https://owncloud.local/s/briefs-diego-2026. Engagement letter is in OnlyOffice My Documents as Engagement_Letter_Diego_Martinez. Ping me on Mattermost anytime.'。使用 /header 将频道头部更新为 'Brand assets, style guides, and design feedback. Contractor onboarding active through 2026-09-30 — welcome Diego Martinez (Mobile App MVP).'。
4. 在 Roundcube 中，检查并设置 Composing Messages 中的 HTML editor 偏好。创建一个新的身份，带有 'Sarah O'Brien — Co-Founder'、'sarah.obrien@mail.local'、'Acme Ventures Inc.' 和 HTML 签名。使用该身份向 'diego.martinez@contractors.local'（CC 'ops@acmeventures.local'）撰写并发送电子邮件，主题为 'Welcome to Acme Ventures — Mobile App MVP Onboarding'，正文如上，并请求 DSN。

**登录凭据：**

- owncloud: admin / admin
- onlyoffice: admin@onlyoffice.local / NewAdmin123!
- mattermost: admin / SeedAdmin1pass
- roundcubemail: james.whitfield@mail.local / User123!
