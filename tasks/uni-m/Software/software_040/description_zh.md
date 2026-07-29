**任务要求：**

为 tabler 项目构建一个前端组件清单。在 code-server 中，打开 tabler 项目，并使用全局 Search 面板（Ctrl+Shift+F），启用 regex，'files to include' 限定为 "tabler/core/**"，'files to exclude' 设为 "**/node_modules/**,**/dist/**"，使用正则表达式 ^\s*export\s+(?:default\s+)?(?:class|function|const)\s+([A-Z][A-Za-z0-9_]*) 来定位每个组件声明。记录每个组件的名称、文件路径和行号。然后在同一个 Search 面板中，清空查询并运行第二个正则搜索，模式为 ^\s*import\s+\{?\s*([A-Z][A-Za-z0-9_]*)\s*\}?\s+from\s+['"][^'"]+['"]（相同范围），找出该项目中每个组件的导入；按组件统计导入该组件的不同文件数量（组件的 usage_count）。在 Baserow 中创建一个名为 "Tabler Component Audit" 的数据库，并创建一个表 "Frontend Components"（字段：Component ID [primary text，格式为 FC-<NNN>，从 FC-001 开始]、Component Name [text]、File Path [text]、Line Number [number]、Usage Count [number]、Category [single-select: Layout/Form/Display/Navigation/Chart/Utility]、Deprecation Candidate [boolean]、Captured At [date]）。按 File Path 字母序然后 Line Number 升序，恰好为每个发现的组件插入一行；Category 从 {"TablerTheme":"Utility","TablerCore":"Utility","NavBar":"Navigation","SideBar":"Navigation","FormInput":"Form","FormSelect":"Form","Card":"Display","Modal":"Display","PageLayout":"Layout","GridContainer":"Layout","BarChart":"Chart","LineChart":"Chart"} 中获取（按 Component Name 键控，缺失时默认为 "Utility"）；当 Usage Count <= 1 时 Deprecation Candidate=true，否则为 false；Captured At=2026-03-20。添加一个名为 "High-Impact Components" 的 Grid 视图，过滤条件为 Usage Count >= 5，并按 Usage Count 降序排序，以及一个按 Category 堆叠的 Kanban 视图 "By Category"。在 code-server 中，在 tabler/docs/COMPONENTS.md 创建一个新文件，内容必须恰好按顺序包含这些行：第 1 行 "# tabler Component Inventory"，第 2 行 "Captured: 2026-03-20"，第 3 行 "Total components: <N>" 其中 N 是插入的行数，第 4 行 "Deprecation candidates: <D>" 其中 D 是 Deprecation Candidate=true 的行数，然后按 Component Name 字母顺序为每个组件输出一行，格式为 "- <Component Name> (<Category>, used <Usage Count>x) — <File Path>:<Line Number>"；使用 Ctrl+S 保存。在 Source Control 面板中，仅暂存这个新文件并以精确提交信息 "docs: add component inventory 2026-03-20" 提交。在 OpenProject 项目 "demo-project" 中，为每个 Deprecation Candidate=true 的 Baserow 行创建恰好一个 Task 类型工作包，subject 为 "Review deprecation: <Component Name>"，assignee 为 OpenProject Admin，priority 为 Normal，description 精确为 "File: <File Path>:<Line Number>; Usage: <Usage Count>; Category: <Category>; Captured: 2026-03-20"。

**步骤：**

1. 在 code-server 中打开 tabler 项目，使用启用 regex 且限定范围的全局 Search 面板运行组件声明正则 ^\s*export\s+(?:default\s+)?(?:class|function|const)\s+([A-Z][A-Za-z0-9_]*); 记录每个匹配的名称、路径和行号。
2. 使用同一范围运行第二个正则 ^\s*import\s+\{?\s*([A-Z][A-Za-z0-9_]*)\s*\}?\s+from\s+['"][^'"]+['"]，并按导入该组件的不同文件数计算 usage_count。
3. 在 Baserow 中创建数据库 "Tabler Component Audit" 和表 "Frontend Components"，使用指定 schema；插入每个组件一行，Category 来自 {"TablerTheme":"Utility","TablerCore":"Utility","NavBar":"Navigation","SideBar":"Navigation","FormInput":"Form","FormSelect":"Form","Card":"Display","Modal":"Display","PageLayout":"Layout","GridContainer":"Layout","BarChart":"Chart","LineChart":"Chart"}，Deprecation Candidate 按规则计算，Captured At=2026-03-20；添加 Grid 视图和 Kanban 视图。
4. 在 code-server 中，创建 tabler/docs/COMPONENTS.md，内容严格遵循指定的行结构（标题、计数，然后按 Component Name 字母序列出每个组件）；保存。
5. 在 Source Control 面板中，仅暂存新的 COMPONENTS.md 文件，并以精确提交信息 "docs: add component inventory 2026-03-20" 提交。
6. 在 OpenProject 项目 "demo-project" 中，为每个 Deprecation Candidate 行创建一个 Task 工作包，使用精确的 subject、assignee OpenProject Admin、priority Normal 和 description 格式。

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- openproject: admin / AdminPass123!
