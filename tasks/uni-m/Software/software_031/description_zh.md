**任务要求：**

为刚关闭的 sprint "Pentest Round 1" 在 OpenProject 项目 "Security Audit" 中执行一次 sprint retrospective 数据收集练习。在 OpenProject 中，打开 "Security Audit" 的 Work packages 列表，按 Version = "Pentest Round 1" 过滤，并记录每个 work package 的 ID、Subject、Type、Status、Estimated Time，以及其 Status 是否为 Closed。在 code-server 中，对于 ["json","data-analyzer"] 中的每个项目，右键单击 Explorer 中的项目文件夹并选择 "Open in Integrated Terminal"，然后执行该项目映射的测试命令 {"json":"cmake --build build --target test","data-analyzer":"pytest tests/ -v"}（这是项目名到测试命令的映射）；解析终端输出，并为每个项目记录：Tests Passed、Tests Failed、Test Files Count（统计该项目 tests/ 目录下的文件数）以及 Pass Rate = round(Tests Passed / (Tests Passed + Tests Failed) * 100, 2)。在 Baserow 中，创建数据库 "Retro Pentest Round 1" 并包含两张表。表 1 "Sprint Work Packages"（字段：WP ID [primary number]、Subject [text]、Type [single-select: Task/Bug/Feature/Epic/Milestone]、Status [text]、Estimated Hours [number with 1 decimal]、Closed [boolean]）。为每个被过滤出的 OpenProject work package 精确插入一行，若 Status 恰好为 "Closed" 则 Closed=true。表 2 "Test Health"（字段：Project [primary single-select，且取值精确为 ["json","data-analyzer"]]、Tests Passed [number]、Tests Failed [number]、Test Files Count [number]、Pass Rate [number with 2 decimals]、Health Badge [single-select: Green/Yellow/Red]）。为 ["json","data-analyzer"] 中的每个项目精确插入一行，当 Pass Rate >= 95 时 Health Badge = "Green"，当 Pass Rate >= 80 时为 "Yellow"，否则为 "Red"。在 "Sprint Work Packages" 上创建一个名为 "Completion Summary" 的 Grid 视图，并按 Closed 分组。在 code-server 中，创建一个新文件 devops-configs/docs/retro-Pentest Round 1.md，且恰好包含六行：第 1 行 "# Retrospective: Pentest Round 1"，第 2 行 "Date: 2024-12-03"，第 3 行 "Work packages closed: <X> of <T>"，其中 X 为 Closed=true 行数，T 为总行数，第 4 行 "Planned hours closed: <P>"，其中 P 为 Closed=true 行的 Estimated Hours 之和（四舍五入到 1 位小数），第 5 行 "Test health — <project>:<Pass Rate>% (<Health Badge>) ; <project>:<Pass Rate>% (<Health Badge>) ; ..."，按 Project 字母顺序列出每个项目，并用 " ; " 分隔，第 6 行 "Red badges: <R>"，其中 R 为 Health Badge = "Red" 的行数；保存文件。在 OpenProject 项目 "Security Audit" 中，创建恰好一个 Task-type work package，主题为 "Retro action items: Pentest Round 1"，assignee 为 OpenProject Admin，priority 为 Normal，描述精确为 "Retro doc: devops-configs/docs/retro-Pentest Round 1.md; Closed rate: <X>/<T>; Red projects: <R>"。

**步骤：**

1. 在 OpenProject 项目 "Security Audit" 中，打开 Work packages 列表，按 Version = "Pentest Round 1" 过滤，并记录每个 work package 的 ID/Subject/Type/Status/Estimated Time。
2. 在 code-server 中，对于 ["json","data-analyzer"] 中的每个项目，打开项目作用域 terminal 并运行映射的测试命令 {"json":"cmake --build build --target test","data-analyzer":"pytest tests/ -v"}；解析输出以提取 Tests Passed、Tests Failed，并统计 tests/ 目录下的 Test Files。
3. 在 Baserow 中，创建数据库 "Retro Pentest Round 1" 以及两张表 "Sprint Work Packages" 和 "Test Health"，字段如上。
4. 用 OpenProject 过滤出的 work package 数据填充 "Sprint Work Packages"（Closed=true 当且仅当 Status="Closed"）。
5. 用 ["json","data-analyzer"] 中每个项目填充 "Test Health"，计算 Pass Rate，并按阈值分配 Health Badge。
6. 在 "Sprint Work Packages" 上创建名为 "Completion Summary" 的 Grid 视图，按 Closed 分组。
7. 在 code-server 中，创建 devops-configs/docs/retro-Pentest Round 1.md，内容严格为描述中的六行，并使用 Baserow 表中计算出的值；保存文件。
8. 在 OpenProject 中，创建一个 Task work package "Retro action items: Pentest Round 1"，assignee 为 OpenProject Admin，priority 为 Normal，并使用指定的描述。

**登录凭据：**

- openproject: admin / AdminPass123!
- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
