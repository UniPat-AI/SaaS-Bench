**任务要求：**

在 Frontend Audit Insights 中开展一次面向前端依赖的技术资产清查和陈旧依赖审计。首先，在 code-server 中打开集成终端，读取 blog-engine/package.json 和 weather-dashboard/package.json；提取每个 package.json 的 'dependencies' 部分中的每一项（不要包含 devDependencies），并保留其精确固定版本字符串。创建一个 Baserow 数据库，名为 "Frontend Dependency Audit 2026"，并包含一个名为 "Dependency Inventory" 的表，字段为 Project (primary text), Dependency Name (text), Current Version (text), Manifest File (text), Captured At (date), Stale (boolean)。为每个提取出的 dependency 插入一行，Captured At 设为 2026-04-15；若 dependency 的 major version 低于 3，或者它出现在 ['express', 'ejs', 'react'] 列表中，则将 Stale 设为 true，否则设为 false。在 Metabase 中，创建一个名为 "Frontend Audit Insights" 的 collection，并在其中针对 Baserow Postgres database 保存两个 questions：(1) "Dependencies by Project" — 一个条形图，显示按 Project 分组的 dependency 数量；(2) "Stale vs Current" — 一个饼图，显示按 Stale 分组的行数。创建一个名为 "Frontend Dependency Health" 的 Metabase dashboard，放在 "Frontend Audit Insights" 中，并将这两个 questions 作为 cards 添加进去。在 OpenProject 项目 "Marketing Website" 中，创建 exactly one type 为 Task 的 work package，subject 为 "Upgrade stale dependencies: 2026-04-15"，priority 为 High，并在 description 中逐行列出每个 stale dependency，格式为 "<Project> / <Dependency Name> @ <Current Version>"。

**步骤：**

1. 在 code-server 中，打开 terminal 并查看 blog-engine/package.json 和 weather-dashboard/package.json；记录 'dependencies' 部分中的每一项（不要包含 devDependencies）及其精确版本字符串。
2. 在 Baserow 中，创建数据库 "Frontend Dependency Audit 2026" 和表 "Dependency Inventory"，字段为 Project, Dependency Name, Current Version, Manifest File, Captured At, Stale；为每个提取出的 dependency 插入一行，Captured At=2026-04-15，并按规则计算 Stale。
3. 在 Metabase 中，创建 collection "Frontend Audit Insights"；针对 Baserow Postgres database 保存两个 questions——条形图 "Dependencies by Project"（按 Project 分组计数）和饼图 "Stale vs Current"（按 Stale 分组计数）。
4. 创建一个 Metabase dashboard "Frontend Dependency Health"，位于 "Frontend Audit Insights" 中，并将两个 saved questions 作为 cards 添加。
5. 在 OpenProject 项目 "Marketing Website" 中，创建一个单独的 Task work package，标题为 "Upgrade stale dependencies: 2026-04-15"，priority 为 High；其 description 必须逐行列出每个 stale dependency，格式为 "<Project> / <Dependency Name> @ <Current Version>"。

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- metabase: admin@metabase.local / mw-admin-123
- openproject: admin / AdminPass123!
