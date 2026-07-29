**任务要求：**

在 3 个项目 ['weather-dashboard','blog-engine','tabler'] 上执行跨项目测试覆盖率审计。在 code-server 中，对于 ['weather-dashboard','blog-engine','tabler'] 中的每个项目，右键单击 File Explorer 中的项目文件夹并选择 'Open in Integrated Terminal'，然后执行该项目的 coverage 命令 {"weather-dashboard":"npx vitest run --coverage","blog-engine":"npx jest --coverage --coverageReporters=text","tabler":"pnpm run test -- --coverage --reporter=text"}（这是项目名到 shell 命令的映射，输出每个模块的 coverage）；解析每次运行，提取恰好属于该项目的以下模块的 coverage percentage {"weather-dashboard":["src/services/geocoding.ts","src/utils/constants.ts"],"blog-engine":["src/services/markdownRenderer.js","src/middleware/logger.js"],"tabler":["core/js/tabler.ts","core/scss/"]}（这是项目名到模块路径列表的映射）。在 Baserow 中，创建数据库 "Coverage Audit Sprint 14 2026" 和两张表。表 1 "Coverage By Module"（字段：Entry ID [primary text，格式化为 CV-<NNN>，从 CV-001 开始]、Project [single-select，且取值精确为 ['weather-dashboard','blog-engine','tabler']]、Module Path [text]、Coverage Pct [number with 2 decimals]、Captured At [date]、Below Threshold [boolean]）——按照 {"weather-dashboard":["src/services/geocoding.ts","src/utils/constants.ts"],"blog-engine":["src/services/markdownRenderer.js","src/middleware/logger.js"],"tabler":["core/js/tabler.ts","core/scss/"]} 定义的每个 (project, module) 对恰好插入一行，顺序为 Project 字母升序，然后 Module Path 字母升序，Captured At=2026-05-20，并且当 Coverage Pct < 70 时 Below Threshold=true。表 2 "Project Coverage Summary"（字段：Project [primary single-select: 取值为 ['weather-dashboard','blog-engine','tabler']]、Module Count [number]、Avg Coverage Pct [number with 2 decimals]、Below Threshold Count [number]）——为每个项目精确插入一行，Avg Coverage Pct = round(该项目各模块 Coverage Pct 的平均值, 2)。在 "Coverage By Module" 上添加一个名为 "Remediation Queue" 的 Grid 视图，过滤为 Below Threshold=true，并按 Coverage Pct 升序排序。在 code-server 中，创建一个新文件 devops-configs/docs/coverage-audit-2026-05-20.md，其内容恰好为以下这些行，顺序不变：第 1 行 "# Coverage Audit — 2026-05-20"，第 2 行 "Projects: <按字母升序排列的项目列表，以逗号分隔>"，然后按项目字母顺序每个项目一行，格式为 "- <Project>: avg <Avg Coverage Pct>% across <Module Count> modules; <Below Threshold Count> below 70%"；保存文件。在 OpenProject 项目 "API Gateway" 中，找出 "Coverage By Module" 中覆盖率最低的 5 个模块（按 Coverage Pct 升序排序，若平局则按 Project 然后 Module Path 字母升序打破平局），并为每个此类模块创建恰好一个 Task-type work package，主题为 "Raise coverage: <Project>/<Module Path> (<Coverage Pct>%)"，assignee 为 Bob Martinez，priority 为 High，描述精确为 "Current: <Coverage Pct>%; Target: 70%; Audit: 2026-05-20"。

**步骤：**

1. 在 code-server 中，对于 ['weather-dashboard','blog-engine','tabler'] 中的每个项目，打开项目作用域的 terminal 并运行映射命令 {"weather-dashboard":"npx vitest run --coverage","blog-engine":"npx jest --coverage --coverageReporters=text","tabler":"pnpm run test -- --coverage --reporter=text"}，捕获每个模块的 coverage 输出。
2. 在 Baserow 中，创建数据库 'Coverage Audit Sprint 14 2026'，以及 'Coverage By Module' 和 'Project Coverage Summary' 两张表，并按所述排序与计算规则填充行。
3. 在 'Coverage By Module' 上添加名为 'Remediation Queue' 的 Grid 视图，过滤为 Below Threshold=true，按 Coverage Pct 升序排序。
4. 在 code-server 中，创建位于 devops-configs/docs/coverage-audit-2026-05-20.md 的报告文件，使用精确的五部分结构并保存。
5. 在 OpenProject 项目 'API Gateway' 中，为覆盖率最低的 5 个模块创建恰好 5 个 Task work package，使用指定主题、assignee、priority 和描述。

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- openproject: admin / AdminPass123!
