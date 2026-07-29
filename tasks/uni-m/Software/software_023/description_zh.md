**任务要求：**

针对库 "typescript"，在项目 ["tabler", "weather-dashboard", "todo-api", "blog-engine"] 上执行一个依赖升级活动。在 code-server 中，使用全局 Search 面板（Ctrl+Shift+F）并启用正则表达式，通过 "files to include" 限定为 "tabler/**,weather-dashboard/**,todo-api/**,blog-engine/**"，定位每一个包含 "typescript" 的 manifest 文件（requirements.txt 或 package.json）；对于每个命中项，打开该文件并记录项目名、manifest 路径以及当前固定版本字符串。在 Baserow 中，创建数据库 "TypeScript Upgrade Campaign July 2026"，并创建表 "Upgrade Inventory"（字段：Project [primary text]、Manifest Path [text]、Current Version [text]、Target Version [text]、Migration Complexity [single-select: Low/Medium/High]、Status [single-select: Pending/InProgress/Done]、Captured At [date]），按 Project 的字母顺序为每个发现的项目恰好插入一行，其中 Current Version 来自 manifest，Target Version = "5.4.5"，Migration Complexity 依据 {"tabler": "High", "weather-dashboard": "Medium", "todo-api": "Low", "blog-engine": "Low"}（以项目名为键）设定，Status = "Pending"，Captured At = 2026-07-08。然后将默认 Grid 视图复制为 "High Complexity"，并添加过滤器 Migration Complexity = High。在 OpenProject 项目 "Mobile App Redesign" 中，创建恰好一个 Epic-type 父级 work package，主题为 "Upgrade typescript to 5.4.5"，priority 为 Normal，描述精确为 "Campaign Date: 2026-07-08; Target: 5.4.5; Projects: <N>"，其中 <N> 为插入的行数；然后为每一条 Status = Pending 的 Baserow 行创建一个 Task-type 子 work package，挂在该 Epic 下，主题为 "[<Project>] Bump typescript <Current Version> → 5.4.5"，assignee 为 OpenProject Admin，若 Migration Complexity = High 则 priority 为 High，否则为 Normal。

**步骤：**

1. 在 code-server 中，使用 Search（Ctrl+Shift+F）并限定到 tabler/**,weather-dashboard/**,todo-api/**,blog-engine/**，以正则查找每一个包含 typescript 的 manifest，并记录每个命中的 Project、Manifest Path 与 Current Version。
2. 在 Baserow 中，创建数据库 TypeScript Upgrade Campaign July 2026 和表 "Upgrade Inventory"，使用指定 schema；按字母顺序为每个项目恰好插入一行，Target Version=5.4.5，Migration Complexity 取 {"tabler": "High", "weather-dashboard": "Medium", "todo-api": "Low", "blog-engine": "Low"}，Status=Pending，Captured At=2026-07-08。
3. 复制默认 grid 视图并命名为 "High Complexity"；添加过滤器 Migration Complexity = High。
4. 在 OpenProject 项目 Mobile App Redesign 中，创建一个 Epic，主题为 "Upgrade typescript to 5.4.5"，描述中包含 campaign date、target 和 project count。
5. 在该 Epic 下，为每一条 Status=Pending 的行创建一个 Task child，使用指定主题、assignee OpenProject Admin，并且 High complexity 行优先级为 High，其余为 Normal。

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- openproject: admin / AdminPass123!
