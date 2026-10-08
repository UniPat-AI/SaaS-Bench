**任务要求：**

作为一名销售运营经理，准备一份客户季度业务回顾演示包，包含性能数据，并在交付给客户之前用于内部审查分发：(1) 在 OnlyOffice 中，在 My Documents 里创建一个名为 'Globex_Industries_Q2_2026_QBR_Data' 的新电子表格。在 Sheet1 中，设置第 1 行表头：Month (A1), Revenue (B1), Support Tickets (C1), SLA Compliance % (D1), NPS Score (E1)。填入 3 行（第 2 到第 4 行）数据，月份名称为 'April, May, June'，性能数据为 187500,54,97.8,58;203000,47,98.6,63;221000,39,99.2,68。在最后一个月份后添加一行，在 A 列写入 'Average'，并在 B 到 E 列使用 AVERAGE 公式。再添加一行，在 A 列写入 'Max'，并在 B 到 E 列使用 MAX 公式。再添加一行，在 A 列写入 'Min'，并在 B 到 E 列使用 MIN 公式。然后在 Common Documents 中创建一个名为 'Globex_Industries_Q2_2026_QBR_Presentation' 的新演示文稿。添加 4 张幻灯片：第 1 张幻灯片标题为 'Globex Industries Q2 2026 Quarterly Business Review'，正文为 'Partnership Review and Growth Strategy - Prepared for Globex Industries Leadership Team'；第 2 张幻灯片标题为 'Q2 2026 Performance Metrics'，正文包含相同的性能数据；第 3 张幻灯片标题为 'Q2 Performance Highlights'，正文为 'Average Revenue: $203,833 | Peak Revenue: $221,000 (June) | Max SLA Compliance: 99.2% | Peak NPS: 68'，突出平均值和最大值；第 4 张幻灯片标题为 'Q3 2026 Proposed Initiatives'，正文为 '1. Roll out Enterprise SSO integration for all Globex users; 2. Deploy custom analytics dashboard with real-time metrics; 3. Establish dedicated technical account management pod'，列出 3 项提议举措。将演示文稿共享给用户 'jun.chen' 以便编辑，并共享给用户 'amit.singh' 以便查看。将演示文稿下载为 PDF 格式。 (2) 在 ownCloud 中，创建一个名为 'Globex_Industries_QBR_Q2_2026' 的文件夹。在其中创建子文件夹 'MetricsData' 和 'SlideDeck'。将下载的 PDF 上传到 'SlideDeck'。在 'MetricsData' 中，创建一个名为 'metric_sources.txt' 的文本文件，内容为 'Revenue: Extracted from HubSpot closed-won deals, aggregated monthly by deal close date for Globex Industries account. | Support Tickets: Pulled from Intercom support export, filtered by Globex organization, counted by creation month. | SLA Compliance %: Derived from Intercom SLA reports, calculated as percentage of tickets meeting first-response and full-resolution SLAs per month. | NPS Score: Collected via monthly AskNicely survey to all Globex Industries power users, averaged and scored using standard NPS methodology.'，列出每个指标的数据来源和采集方法。将 'Globex_Industries_QBR_Q2_2026' 与组 'admin' 以读写权限共享。将 'SlideDeck' 单独与组 'admin' 以只读权限共享。获取 'SlideDeck' 的私有链接。查看 Shared with others 视图，以确认两个共享都已出现。 (3) 在 Mattermost 中，在 Product & Design 团队里，创建一个名为 'globex-qbr-q2-review' 的新私有频道，频道头部为 'Internal review channel for Globex Industries Q2 2026 QBR deliverables'，目的为 'Coordinate internal review and approval of the Globex Industries Q2 2026 QBR presentation and supporting data before client delivery'。在频道中发布消息 'Team, please review the Globex Industries Q2 2026 QBR package at the following private link. Review and sign-off needed by 2026-07-22 EOD.'，包含第 2 步中的私有链接，并请求在 '2026-07-22 EOD' 前审查。然后使用 @mention 在线程回复中标记 'christene'，文本为 '@christene Could you please verify the technical accuracy of the SLA compliance figures and ticket volume counts in slides 2 and 3? Need your sign-off before we send to the client.'，请求验证技术准确性。为原始审查请求消息添加表情反应 'thumbsup'。 (4) 在 Roundcube 中，导航到 Settings > Identities，创建一个新身份，显示名称为 'Marcus Torres - Sales Operations'，邮箱为 'marcus.torres@mail.local'，organization 为 'Sales Operations - TechCorp'，并设置一个纯文本签名 'Marcus Torres
Sales Operations Manager
TechCorp
marcus.torres@mail.local
+1-555-0187'。然后使用该身份撰写一封电子邮件给 'linda.park@globexindustries.com'，CC 为 'emma.larsson@mail.local'，主题为 'Globex Industries Q2 2026 Quarterly Business Review - Proposed Meeting Date'，正文为 'Hi Linda,

I hope you are doing well. I would like to propose a Quarterly Business Review meeting on Wednesday, July 29, 2026 at 10:00 AM ET to review Globex Industries'' Q2 2026 performance and discuss our path forward.

Proposed agenda (4 topics based on our presentation slides):
1. Globex Industries Q2 2026 Quarterly Business Review - partnership overview and meeting context
2. Q2 2026 Performance Metrics - revenue, tickets, SLA, and NPS review
3. Q2 Performance Highlights - average and peak performance indicators
4. Q3 2026 Proposed Initiatives - proposed next-quarter investments

Please confirm if this time works, or propose an alternative that fits your schedule.

Best regards,
Marcus'，提议 QBR 会议日期并列出来自演示文稿的 4 个议程主题。将消息优先级设为 Normal。发送邮件。然后导航到 Settings > Preferences > Contacts，并将新联系人的默认地址簿设置为 'Personal Addresses'。然后在 Personal Address Book 中添加一个新联系人，名字为 'Linda'，姓氏为 'Park'，邮箱为 'linda.park@globexindustries.com'，organization 为 'Globex Industries'。

**步骤：**

1. 在 OnlyOffice 中，创建一个带有月度指标、AVERAGE/MAX/MIN 公式的性能数据电子表格。创建一份包含性能数据文本和举措幻灯片的 QBR 演示文稿。共享演示文稿并将其下载为 PDF。
2. 在 ownCloud 中，创建一个包含 data 和 presentation 子文件夹的 QBR 文件夹。上传 PDF。创建一个数据来源文本文件。与销售组（读写）和 exec 组（只读）共享。获取私有链接并验证共享。
3. 在 Mattermost 中，创建一个私有 QBR 审查频道，发布带有私有链接的审查请求，在线程回复中标记 solutions engineer，并对原消息作出反应。
4. 在 Roundcube 中，创建一个带签名的新发件人身份。使用新身份撰写并发送 QBR 会议提议邮件。配置默认地址簿并将客户添加为新联系人。

**登录凭据：**

- onlyoffice: admin@onlyoffice.local / NewAdmin123!
- owncloud: admin / admin
- mattermost: admin / SeedAdmin1pass
- roundcubemail: james.whitfield@mail.local / User123!
