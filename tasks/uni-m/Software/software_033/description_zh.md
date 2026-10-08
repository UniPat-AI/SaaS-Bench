**任务要求：**

审计并修复 devops-configs 项目的 CI/CD pipeline。在 code-server 中，打开文件 devops-configs/.github/workflows/deploy.yml；使用 Find（Ctrl+F）定位顶层 'jobs:' mapping，并记录其下直接定义的每个 job name（即在 'jobs:' 下方一层缩进的名称），以及每个 job 的 'runs-on' 值和是否声明了 'needs:' key。然后在同一文件中，使用 Find and Replace（Ctrl+H）并启用正则模式，将精确 token "ubuntu-18.04" 在整个文件中替换为 "ubuntu-22.04"，保存文件（Ctrl+S），并在 Source Control 面板中仅 stage devops-configs/.github/workflows/deploy.yml，提交时使用精确消息 "ci: upgrade runner to ubuntu-22.04"。在 Baserow 中，创建数据库 "CI Workflow Remediation Tracker" 和表 "CI Jobs"（字段：Job ID [primary text，格式化为 CJ-<NN>，从 CJ-01 开始]、Job Name [text]、Runs On [text]、Has Dependencies [boolean]、Stage Category [single-select: Build/Test/Lint/Deploy/Other]、Missing Stage [boolean]）；按文件中出现的顺序为每个提取到的 job 精确插入一行（从上到下），并根据 Job Name 使用 {"docker-build": "Build", "npm-build": "Build", "jest": "Test", "e2e": "Test", "prettier": "Lint", "tflint": "Lint", "deploy-staging": "Deploy", "deploy-prod": "Deploy", "notify": "Other"} 进行 Stage Category 映射，若 job 不在映射中则使用 Other；并且对于在插入行中未出现的每个必需 stage（Build、Test、Lint、Deploy），将 Missing Stage=true，且为每个缺失的必需 stage 额外创建一行，Job Name="MISSING:<stage>"，Runs On=""，Has Dependencies=false，Stage Category=<stage>，Missing Stage=true。添加一个名为 "Gaps" 的 Grid 视图，过滤条件为 Missing Stage=true。在 OpenProject 项目 "devops-automation" 中，为每一条 Missing Stage=true 的行创建恰好一个 Task-type work package，主题为 "Add CI stage: <Stage Category>"，assignee 为 Paul Harris，priority 为 High，描述精确为 "Add a job of category <Stage Category> to devops-configs/.github/workflows/deploy.yml; current jobs: <comma-separated list of Job Name values where Missing Stage=false, sorted alphabetically>"。

**步骤：**

1. 在 code-server 中，打开 devops-configs/.github/workflows/deploy.yml，并列出 jobs 顶层映射下的所有 job name、runs-on 值以及 needs 声明。
2. 在 code-server 中，使用 Find and Replace（Ctrl+H）并启用正则，将 devops-configs/.github/workflows/deploy.yml 中的 'ubuntu-18.04' 替换为 'ubuntu-22.04'，保存，并通过 Source Control 面板使用精确消息 'ci: upgrade runner to ubuntu-22.04' 提交。
3. 在 Baserow 中，创建数据库 'CI Workflow Remediation Tracker' 和 'CI Jobs' 表 schema，并按映射 {"docker-build": "Build", "npm-build": "Build", "jest": "Test", "e2e": "Test", "prettier": "Lint", "tflint": "Lint", "deploy-staging": "Deploy", "deploy-prod": "Deploy", "notify": "Other"} 为每个发现的 job 插入一行。
4. 对于每个未被现有 job 覆盖的必需 stage（Build/Test/Lint/Deploy），插入一个 Missing Stage=true 的占位行；并添加一个过滤为 Missing Stage=true 的 Grid 视图 'Gaps'。
5. 在 OpenProject 'devops-automation' 中，为每个 Missing Stage=true 行创建一个 Task work package，使用指定的主题、assignee、priority 和描述。

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- openproject: admin / AdminPass123!
