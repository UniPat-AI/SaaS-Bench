**任务要求：**

为项目 "E-Commerce Platform" 启动一个新的 sprint "Sprint 2025-04"，并进行多工件设置。在 OpenProject 项目 "E-Commerce Platform" 中，导航到 Versions (Roadmap) 页面并创建名为 "Sprint 2025-04" 的 version，status=open，start date=2025-04-07，due date=2025-04-18，描述精确为 "Sprint goal: Stabilize checkout and improve cart conversion"。然后导航到 Boards 并创建一个基于 Status 的新 board，标题为 "Sprint 2025-04 Board"，使用默认状态列。对于 [{"subject": "Fix race condition in cart merge on login", "type": "Bug", "priority": "High", "estimated_hours": 6.0, "assignee": "OpenProject Admin", "target_module": "blog-engine/src/routes/api.js"}, {"subject": "Add structured logging to checkout routes", "type": "Task", "priority": "Normal", "estimated_hours": 4.5, "assignee": "John Marshall", "target_module": "todo-api/tests/test_categories.py"}, {"subject": "Slug generation helper supports unicode", "type": "Feature", "priority": "Normal", "estimated_hours": 8.0, "assignee": "Lena Hogan", "target_module": "blog-engine/src/utils/slugify.js"}, {"subject": "Export analyzer summary as JSON", "type": "Feature", "priority": "Low", "estimated_hours": 5.0, "assignee": "Jane Dradder", "target_module": "data-analyzer/src/analyzer.py"}, {"subject": "Fix ItemList pagination double-fetch", "type": "Bug", "priority": "Immediate", "estimated_hours": 3.0, "assignee": "Latisha Mazon", "target_module": "vue-hackernews-2.0/src/views/ItemList.vue"}] 中的每个条目（这是一个包含 subject、type、priority、estimated_hours、assignee、target_module 字段的 JSON 数组对象），在 OpenProject 项目 "E-Commerce Platform" 中恰好创建一个 work package，并将其分配到 version "Sprint 2025-04"，属性（Type、Subject、Priority、Estimated Time、Assignee）全部与数据完全一致。创建完所有 work package 之后，在项目 "E-Commerce Platform" 的 Meetings 页面上创建恰好一个一次性 meeting，标题为 "Sprint Planning: Sprint 2025-04"，时间为 2025-04-07 10:00，并按照 backlog 条目的顺序添加恰好 5 个 agenda items——每个条目一个，标题为 "Review: <subject>"。在 code-server 中，打开 File Explorer，并对每个 backlog 条目打开其 target_module 路径中的文件；使用 Find（Ctrl+F）定位文件的第一个非空行，并在该行正上方插入恰好一行注释，格式为 "# TODO [Sprint 2025-04]: <subject>"（当文件为 .js、.ts、.tsx 或 .vue 时使用 "// TODO" 代替 "# TODO"），然后保存。在 Baserow 中，创建数据库 "Sprint 2025-04 Tracking" 和表 "Sprint Backlog"（字段：Item ID [primary text，格式化为 SB-<NN>，从 SB-01 开始]、Subject [text]、Type [single-select: Task/Bug/Feature/Epic]、Priority [single-select: Low/Normal/High/Immediate]、Estimated Hours [number with 1 decimal]、Assignee [text]、Target Module [text]、OpenProject WP ID [number]）。精确插入 5 行，每个 backlog 条目一行，并按 backlog JSON 的相同顺序插入，OpenProject WP ID 设为 OpenProject 创建时分配的数值 work package ID。添加一个名为 "By Priority" 的 Kanban 视图，并按 Priority 分组。

**步骤：**

1. 在 OpenProject 项目 "E-Commerce Platform" 中，在 Versions (Roadmap) 页面创建 version "Sprint 2025-04"，设置 status=open、start date 2025-04-07、due date 2025-04-18，并将描述设为 "Sprint goal: Stabilize checkout and improve cart conversion"。
2. 在同一项目中，创建一个新的基于 Status 的 board，标题为 "Sprint 2025-04 Board"，使用默认状态列。
3. 对于 5 个 backlog 条目中的每一个，在 "E-Commerce Platform" 中创建一个 work package，并将其分配到 version "Sprint 2025-04"，且 Type/Subject/Priority/Estimated Time/Assignee 与数据完全一致。
4. 在 "E-Commerce Platform" 的 Meetings 页面上，创建一个一次性 meeting "Sprint Planning: Sprint 2025-04"，时间为 2025-04-07 10:00，并按 backlog 顺序为每个条目添加一个标题为 "Review: <subject>" 的 agenda item。
5. 在 code-server 中，对于每个 backlog 条目，打开其 target_module 路径中的文件，使用 Find 找到第一个非空行，并在其正上方插入正确格式的 TODO 注释行（.py 文件用 #，.js/.ts/.tsx/.vue 文件用 //），然后保存。
6. 在 Baserow 中，创建数据库 "Sprint 2025-04 Tracking" 和 "Sprint Backlog" 表，字段如上；按 backlog 顺序插入 5 行，并包含 OpenProject 实际分配的 WP ID。
7. 在 "Sprint Backlog" 上添加一个名为 "By Priority" 的 Kanban 视图，按 Priority 分组。

**登录凭据：**

- openproject: admin / AdminPass123!
- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
