**任务要求：**

执行一条活动容量管理工作流，涵盖配额共享、基于优惠券的 VIP 优先级，以及横跨 Pretix 活动设置、客户账户管理和 Twenty CRM 记录/任务创建的 CRM 驱动跟进协调。

在 Pretix 中：(1) 创建一个新的活动 'Brooklyn Jazz Symposium 2026'，slug 为 'bklyn-jazz-symposium-2026'，归属于组织者 'urban-music'，开始日期为 2026-10-18，货币为 'USD'。 (2) 创建两个分类：'General Admission' 和 'Experience Add-ons'。 (3) 在 'General Admission' 分类下创建产品 'General Admission Pass'，价格为 85。 (4) 在 'General Admission' 分类下创建产品 'VIP Backstage Pass'，价格为 220。 (5) 在 'Experience Add-ons' 分类下创建产品 'Jam Session Workshop'，价格为 60。 (6) 创建一个共享配额 'Main Venue Capacity'，大小为 350，并同时关联到 'General Admission Pass' 和 'VIP Backstage Pass'。这意味着标准票和高级票的总售出量不能超过 350。 (7) 创建一个单独的配额 'Jam Session Quota'，大小为 50，并关联到 'Jam Session Workshop'。 (8) 创建一个类型为 'Text (one line)' 的自定义问题，文本为 'Company Affiliation'，并将其设为 'General Admission Pass' 和 'VIP Backstage Pass' 的必填项。 (9) 创建第二个自定义问题，类型为 'Choice (single)'，文本为 'Dietary Preference'，选项为 'Vegetarian'、'Gluten-Free'、'No Preference'，并将其设为 'VIP Backstage Pass' 的必填项。 (10) 创建一个代码为 'VIPJAZZ2026' 的优惠券，提供 30% 折扣，最大使用次数 8 次，有效期至 2026-10-18，关联到 'VIP Backstage Pass' 和 'Jam Session Workshop'。 (11) 创建第二个优惠券代码 'GROUPJAZZ40'，提供 40 USD 的固定折扣，最大使用次数 25 次，有效期至 2026-09-30，关联到 'General Admission Pass'。 (12) 创建名为 'Jazz Early Bird' 的折扣规则，并将 20% 折扣应用于 'General Admission Pass'。 (13) 创建名为 'Main Venue Check-In' 的签到名单，关联到 'General Admission Pass' 和 'VIP Backstage Pass'。 (14) 创建第二个签到名单 'Jam Session Check-In'，仅关联到 'Jam Session Workshop'。 (15) 打开 Event Display Settings。上传文件 '/tmp/jazz_event_logo.png' 作为活动 logo，并将主色设置为 '#7C3AED'。将自定义首页文案设置为 'Welcome to the Brooklyn Jazz Symposium 2026 — an unforgettable evening of world-class jazz performances and immersive workshops.'。 (16) 打开 Event Invoice Settings。启用自动生成发票。将发票号前缀设置为 'BJS2026-'。 (17) 将活动设为公开。 (18) 打开 Event Dashboard。验证活动处于公开状态。生成活动二维码，并确认仪表板显示 'Download QR code as PNG' 选项，或者 PNG 文件已成功下载。

在 Pretix（客户管理）中：(19) 打开组织者 'urban-music' 的 Organizer Customers 页面。创建一个名为 'Helena Vasquez'、邮箱为 'helena.vasquez@jazzpremier.com' 的新客户账户。 (20) 创建第二个客户账户，名称为 'Dominic Ferrara'，邮箱为 'dominic.ferrara@soundwavecorp.com'。 (21) 打开 Organizer Membership Types 页面。创建一个名为 'Urban Music VIP Patron' 的会员类型。 (22) 打开 'Helena Vasquez' 的客户详情页。添加一个类型为 'Urban Music VIP Patron' 的会员，开始日期为 2026-03-01，结束日期为 2027-02-28。

在 Twenty CRM 中：(23) 创建公司 'Jazz Premier Group'，域名为 'jazzpremier.com'。 (24) 创建人员 'Helena Vasquez'，邮箱为 'helena.vasquez@jazzpremier.com'，职位为 'Director of Cultural Programming'，并关联到公司 'Jazz Premier Group'。 (25) 创建公司 'Soundwave Corporation'，域名为 'soundwavecorp.com'。 (26) 创建人员 'Dominic Ferrara'，邮箱为 'dominic.ferrara@soundwavecorp.com'，职位为 'Head of Artist Relations'，并关联到公司 'Soundwave Corporation'。 (27) 对列表 [{'name': 'Rhythm House Productions', 'domain': 'rhythmhouseprod.com', 'contact_name': 'Amara Diallo', 'contact_email': 'amara.diallo@rhythmhouseprod.com'}, {'name': 'Blue Note Ventures', 'domain': 'bluenoteven.com', 'contact_name': 'Stefan Kowalczyk', 'contact_email': 'stefan.kowalczyk@bluenoteven.com'}]（恰好 2 条）中的每个条目，使用该条目的 'name' 和 'domain' 字段创建一条公司记录。然后使用该条目的 'contact_name' 和 'contact_email' 字段创建一个人员，并关联到该公司。 (28) 对 waitlist companies 中的每个条目，创建一个关联到该公司的任务，标题为 'Notify when capacity opens - Brooklyn Jazz Symposium 2026 - [company name]'（其中 [company name] 替换为条目的 'name'），到期日为 2026-09-15，正文为：'Contact is on the waiting list for Brooklyn Jazz Symposium 2026 (2026-10-18). Venue capacity: 350. Monitor quota availability. Voucher for group booking: GROUPJAZZ40 (40 USD off, max 25 uses). Send registration link when spots become available.' (29) 创建一个标题为 'Send VIP invitations - Brooklyn Jazz Symposium 2026' 的任务，到期日为 2026-08-20，正文为：'Send exclusive invitations to VIP contacts:
- Helena Vasquez (helena.vasquez@jazzpremier.com) at Jazz Premier Group - voucher: VIPJAZZ2026 (30% off premium + workshop)
- Dominic Ferrara (dominic.ferrara@soundwavecorp.com) at Soundwave Corporation - voucher: VIPJAZZ2026
Membership: Helena Vasquez has Urban Music VIP Patron membership active until 2027-02-28.' (30) 创建一条标题为 'Brooklyn Jazz Symposium 2026 - Capacity & Pricing Summary' 的笔记，正文为：'Event: Brooklyn Jazz Symposium 2026 - 2026-10-18
Venue capacity: 350 (shared between General Admission Pass and VIP Backstage Pass)
Workshop capacity: 50

Pricing:
- General Admission Pass: 85 USD (early-bird 20% off)
- VIP Backstage Pass: 220 USD
- Jam Session Workshop: 60 USD

Vouchers:
- VIP: VIPJAZZ2026 - 30% off premium+workshop, max 8 uses
- Group: GROUPJAZZ40 - 40 USD off standard, max 25 uses

VIP customers: Helena Vasquez (Urban Music VIP Patron member), Dominic Ferrara
Waitlisted companies: 2
Invoice prefix: BJS2026-'

**步骤：**

1. 在 Pretix 中创建活动 'Brooklyn Jazz Symposium 2026'（slug 'bklyn-jazz-symposium-2026'，组织者 'urban-music'，日期 2026-10-18，货币 'USD'）。创建分类 'General Admission' 和 'Experience Add-ons'。创建产品：'General Admission Pass'，价格 85，属于 'General Admission'；'VIP Backstage Pass'，价格 220，属于 'General Admission'；'Jam Session Workshop'，价格 60，属于 'Experience Add-ons'。创建共享配额 'Main Venue Capacity'（大小 350），关联标准票和高级票。创建配额 'Jam Session Quota'（大小 50），关联 workshop 附加项。
2. 在 Pretix 中创建自定义问题 'Company Affiliation'（Text, one line），作为标准票和高级票的必填项。创建自定义问题 'Dietary Preference'（Choice, single），选项为 'Vegetarian'、'Gluten-Free'、'No Preference'，仅作为高级票必填项。创建优惠券 'VIPJAZZ2026'（30% off，最多 8 次，有效期至 2026-10-18），关联高级票和 workshop。创建优惠券 'GROUPJAZZ40'（40 USD 固定折扣，最多 25 次，有效期至 2026-09-30），关联标准票。创建折扣规则 'Jazz Early Bird'（20% off），应用于标准票。
3. 在 Pretix 中创建签到名单 'Main Venue Check-In'，关联标准票和高级票。创建签到名单 'Jam Session Check-In'，仅关联 workshop 附加项。在 Event Display Settings 中，上传 logo 文件 '/tmp/jazz_event_logo.png'，将主色设为 '#7C3AED'，并将首页文案设为 'Welcome to the Brooklyn Jazz Symposium 2026 — an unforgettable evening of world-class jazz performances and immersive workshops.'。在 Invoice Settings 中，启用自动发票生成并将前缀设为 'BJS2026-'。将活动设为公开。在 Dashboard 上验证公开状态，并确认二维码 PNG 生成可用（仪表板显示 'Download QR code as PNG' 或 PNG 成功下载）。
4. 在 Pretix Organizer Customers 中，创建客户 'Helena Vasquez'（helena.vasquez@jazzpremier.com）和客户 'Dominic Ferrara'（dominic.ferrara@soundwavecorp.com）。在 Organizer Membership Types 中，创建类型 'Urban Music VIP Patron'。在 'Helena Vasquez' 的详情页上，将 'Urban Music VIP Patron' 会员从 2026-03-01 添加到 2027-02-28。
5. 在 Twenty CRM 中，创建公司 'Jazz Premier Group'（域名 'jazzpremier.com'）。创建人员 'Helena Vasquez'（邮箱 'helena.vasquez@jazzpremier.com'，职位 'Director of Cultural Programming'）并关联到 'Jazz Premier Group'。创建公司 'Soundwave Corporation'（域名 'soundwavecorp.com'）。创建人员 'Dominic Ferrara'（邮箱 'dominic.ferrara@soundwavecorp.com'，职位 'Head of Artist Relations'）并关联到 'Soundwave Corporation'。对于 waitlist 中的 2 条记录中的每一条，创建一家公司（name, domain）和一个人员（contact_name, contact_email）并关联到该公司：'Rhythm House Productions'（rhythmhouseprod.com，联系人 Amara Diallo，amara.diallo@rhythmhouseprod.com）以及 'Blue Note Ventures'（bluenoteven.com，联系人 Stefan Kowalczyk，stefan.kowalczyk@bluenoteven.com）。
6. 在 Twenty CRM 中，对 waitlist companies 中的每个条目，创建一个关联到相应公司的任务，标题为 'Notify when capacity opens - Brooklyn Jazz Symposium 2026 - [company name]'，到期日 2026-09-15，正文包含：'Contact is on the waiting list for Brooklyn Jazz Symposium 2026 (2026-10-18). Venue capacity: 350. Monitor quota availability. Voucher for group booking: GROUPJAZZ40 (40 USD off, max 25 uses). Send registration link when spots become available.' 创建任务 'Send VIP invitations - Brooklyn Jazz Symposium 2026'，到期日 2026-08-20，正文列出两个 VIP 联系人及其邮箱、公司、优惠券代码 'VIPJAZZ2026'（30% off），以及关于 'Helena Vasquez'（Urban Music VIP Patron 有效期至 2027-02-28）的会员说明。创建一条标题为 'Brooklyn Jazz Symposium 2026 - Capacity & Pricing Summary' 的笔记，正文包含：活动名 'Brooklyn Jazz Symposium 2026' 和日期 '2026-10-18'，场馆容量 350（由 'General Admission Pass' 和 'VIP Backstage Pass' 共享），workshop 容量 50，所有三种产品的价格及货币 'USD'，early-bird 20% 说明，VIP 优惠券 'VIPJAZZ2026' 详情（30%，最多 8 次），group 优惠券 'GROUPJAZZ40' 详情（40 USD，最多 25 次），VIP 客户姓名 'Helena Vasquez'（Urban Music VIP Patron member）和 'Dominic Ferrara'，waitlisted company 数量 '2'，以及发票前缀 'BJS2026-'。

**登录凭证：**

- pretix: admin@localhost / admin
- twenty: jony.ive@apple.dev / tim@apple.dev

