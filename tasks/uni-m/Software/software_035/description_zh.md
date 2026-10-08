**任务要求：**

在 OpenProject 项目 "API Gateway" 中协调一次 v1.5.0 发布，并进行变更日志管理和就绪跟踪。进入 Versions (Roadmap) 并创建一个名为 "v1.5.0" 的版本，status=open，start date=2025-08-05，due date=2025-09-20，description 精确为 "Release v1.5.0 coordinated across vue-hackernews-2.0 and tabler"。在 code-server 中，在项目 vue-hackernews-2.0 里打开 vue-hackernews-2.0/CHANGELOG.md，并在编辑器中找到精确行 "## [Unreleased]"；将这一单行替换为恰好两行新内容：第 A 行 "## [Unreleased]"（保留在顶部）和第 B 行 "## [v1.5.0] - 2025-09-20" 紧随其下，然后在第 B 行下方再插入恰好三行，格式为 "### Added"、"- Infinite scroll pagination for story lists"、"- Dark mode toggle with localStorage persistence"；保存文件。在项目 tabler 中对 tabler/CHANGELOG.md 重复完全相同的结构修改，使用 Accessible color contrast audit across all components 和 New timeline component with responsive variants。 在 Source Control 面板中，对这两个修改过的文件分别单独暂存，并使用精确提交信息 "docs(changelog): prepare v1.5.0" 提交。在 Baserow 中创建一个数据库 "Release v1.5.0 Coordination"，其中包含一个表 "Release Readiness"（字段：Gate ID [primary text，格式为 G-<NN>，从 G-01 开始]、Gate Name [single-select: CodeFreeze/QASignoff/StagingDeploy/ProductionDeploy]、Project [single-select: vue-hackernews-2.0/tabler/Both]、Target Date [date]、Status [single-select: NotStarted/InProgress/Done/Blocked]、Owner [text]），并且恰好插入 8 行——每个项目各四个 gate，适用于 vue-hackernews-2.0 和 tabler（顺序为：CodeFreeze/vue-hackernews-2.0、CodeFreeze/tabler、QASignoff/vue-hackernews-2.0、QASignoff/tabler、StagingDeploy/vue-hackernews-2.0、StagingDeploy/tabler、ProductionDeploy/vue-hackernews-2.0、ProductionDeploy/tabler）——Target Date 取自 {"CodeFreeze": "2025-08-28", "QASignoff": "2025-09-06", "StagingDeploy": "2025-09-13", "ProductionDeploy": "2025-09-20"}（按 Gate Name 键控），Status=NotStarted，Owner 取自 {"CodeFreeze": "Eric Rothman", "QASignoff": "Richard Rethman", "StagingDeploy": "Thomas Nickson", "ProductionDeploy": "Sandra Love"}（按 Gate Name 键控）。添加一个名为 "Gate Progress" 的 Kanban 视图，按 Status 分组堆叠。在 OpenProject 项目 "API Gateway" 中，恰好创建 8 个 Milestone 类型工作包——每个 Baserow 行对应一个——subject 为 "[<Gate Name>] <Project>: v1.5.0"，分配到版本 "v1.5.0"，start date = Target Date，若 Gate Name 为 (StagingDeploy, ProductionDeploy) 则 priority 为 High，否则为 Normal，description 精确为 "Release: v1.5.0; Gate: <Gate Name>; Project: <Project>; Owner: <Owner>"。

**步骤：**

1. 在 OpenProject 项目 'API Gateway' 中，进入 Versions (Roadmap) 页面，使用指定的 status、日期和 description 创建版本 'v1.5.0'
2. 在 code-server 中，按指定方式编辑 vue-hackernews-2.0/CHANGELOG.md 和 tabler/CHANGELOG.md，保存两者，然后通过 Source Control 以精确提交信息 'docs(changelog): prepare v1.5.0' 分别暂存并提交每个文件
3. 在 Baserow 中创建数据库 'Release v1.5.0 Coordination'，包含 'Release Readiness' 表，并按指定顺序插入恰好 8 行 gate 记录，使用 {"CodeFreeze": "2025-08-28", "QASignoff": "2025-09-06", "StagingDeploy": "2025-09-13", "ProductionDeploy": "2025-09-20"} 和 {"CodeFreeze": "Eric Rothman", "QASignoff": "Richard Rethman", "StagingDeploy": "Thomas Nickson", "ProductionDeploy": "Sandra Love"}
4. 在 'Release Readiness' 表上添加按 Status 堆叠的 Kanban 视图 'Gate Progress'
5. 在 OpenProject 中，创建恰好 8 个与 8 条 Baserow 记录对应的 Milestone 工作包，使用指定的 subject、priority、日期和 description，全部分配到版本 'v1.5.0'

**登录凭据：**

- openproject: admin / AdminPass123!
- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
