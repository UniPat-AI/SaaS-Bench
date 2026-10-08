**任务要求：**

对项目 ['todo-api', 'data-analyzer', 'blog-engine', 'weather-dashboard'] 执行代码质量审计，强制执行编码标准。在 code-server 中，对于 ['todo-api', 'data-analyzer', 'blog-engine', 'weather-dashboard'] 里的每个项目，右键单击 Explorer 中的项目文件夹并选择 "Open in Integrated Terminal"，然后运行 {'todo-api': 'flake8 .', 'data-analyzer': 'flake8 .', 'blog-engine': 'npx eslint .', 'weather-dashboard': 'npx eslint . --ext .ts,.tsx'}（这是项目名到 lint 命令的映射）。捕获 linter 输出，并编写一个脚本（例如在 code-server 中编写 Python 脚本）将每个 violation 解析为列（Project、File Path、Rule ID、Severity），并生成名为 "lint_violations.csv" 的 CSV 文件，包含 ['todo-api', 'data-analyzer', 'blog-engine', 'weather-dashboard'] 中所有 violation（每个 violation 一行）。在 Baserow 中，创建数据库 "Code Quality Audit Q2 2025" 并导入 "lint_violations.csv" 以创建名为 "Lint Violations" 的表。确保该表具有以下字段：Violation ID（primary text，格式化为 LV-<NNN>，按 CSV 行顺序从 LV-001 连续编号）、Project（single-select，取值来自 ['todo-api', 'data-analyzer', 'blog-engine', 'weather-dashboard']）、File Path（text）、Rule ID（text）、Severity（single-select: Error/Warning/Info）、Captured At（date）。在导入前或导入后通过批量更新，将 Violation ID 连续编号（LV-001、LV-002、...），并将每一行的 Captured At 设为 2025-05-14。添加一个名为 "Top Offenders" 的 Grid 视图，按 File Path 分组（在 footer 中使用 row-count 聚合来查看计数），并添加一个过滤器 Severity = Error。在 Metabase 中，创建一个名为 "Lint Audit Q2 2025" 的 collection，并在 Admin → Databases 中为 Baserow Postgres 数据库触发手动 schema sync，以便 "Lint Violations" 表可见。在该 collection 中，保存两条针对 Baserow Postgres 数据库的 question：(1) "Violations by Project" —— 按 Project 分组、按 Severity 展开的 violation 数量柱状图；(2) "Rule Frequency" —— 列出 Rule ID 和 count 的表格，按 count 降序排序，并限制为前 10 行。创建一个名为 "Code Quality Audit Dashboard" 的 Metabase dashboard，位于同一 collection 中，并将这两个 question 作为卡片添加进去，dashboard description 精确为 "Lint audit 2025-05-14 across 4 projects"。在 OpenProject 项目 "Security Audit" 中，找出 Error-severity violation 最多的前 5 个文件（按 error count 降序排序，若平局则按 File Path 升序）并为每个此类文件创建一个 Task-type work package，主题为 "Fix lint errors: <File Path> (<E> errors)"，assignee 为 OpenProject Admin，priority 为 High，描述精确为 "Project: <Project>; Lint summary: <summary of the lint findings for that file>"。

**步骤：**

1. 在 code-server 中，为 ['todo-api', 'data-analyzer', 'blog-engine', 'weather-dashboard'] 中的每个项目打开一个项目作用域的 integrated terminal，并运行对应命令 {'todo-api': 'flake8 .', 'data-analyzer': 'flake8 .', 'blog-engine': 'npx eslint .', 'weather-dashboard': 'npx eslint . --ext .ts,.tsx'}；将每个 linter 的输出保存到文件。
2. 在 code-server 中，编写并运行一个脚本，解析捕获到的 linter 输出并生成单个 CSV 文件 "lint_violations.csv"，其列为 Project、File Path、Rule ID、Severity（所有项目中的每个 violation 一行）。
3. 在 Baserow 中，创建数据库 Code Quality Audit Q2 2025 并导入 "lint_violations.csv" 以创建 "Lint Violations" 表；按照指定配置字段类型（Violation ID primary text，Project 和 Severity 为 single-select，Captured At 为 date），并将 Violation ID 按 CSV 行顺序设为 LV-001、LV-002、...，Captured At 对每一行设为 2025-05-14。
4. 添加一个名为 "Top Offenders" 的 Grid 视图，按 File Path 分组，并在 footer 中加入 row-count 聚合，同时添加过滤器 Severity=Error。
5. 在 Metabase 中，为 Baserow Postgres 数据库触发 schema sync，创建 collection Lint Audit Q2 2025，并保存两条指定 question（"Violations by Project" 柱状图和 "Rule Frequency" 前 10 表格）。
6. 在同一 collection 中创建 dashboard Code Quality Audit Dashboard，描述精确为 "Lint audit 2025-05-14 across 4 projects"，并将两个 question 作为卡片添加进去。
7. 在 OpenProject Security Audit 中，为 Error-severity violation 最多的前 5 个文件创建 5 个 Task work package（按 File Path 升序打破平局），使用指定主题、assignee OpenProject Admin、priority High，以及描述格式 "Project: <Project>; Lint summary: <summary of the lint findings for that file>"。

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- metabase: admin@metabase.local / mw-admin-123
- openproject: admin / AdminPass123!
