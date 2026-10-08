**任务要求：**

建立一个 Architecture Decision Record (ADR) governance workflow。在 code-server 中，创建一个新文件夹位于 devops-configs/docs/adr/，并在其中创建 exactly 3 个 markdown 文件，命名为 ['005-secrets-management.md', '006-container-orchestration.md', '007-backup-strategy.md']。每个文件必须严格包含五行，顺序如下：第 1 行 "# ADR-<NNN>: <Title>"（其中 NNN 是文件名中的零填充编号，Title 是来自 ['Secrets Management Solution for Production Workloads', 'Container Orchestration Platform Selection', 'Backup and Disaster Recovery Strategy'] 的对应条目），第 2 行 "Status: Review"，第 3 行 "Date: 2025-06-10"，第 4 行 "Author: DevOps Engineering Guild"，第 5 行为来自 ['We need to centralize secret storage and rotation to eliminate plaintext credentials from source control and improve auditability.', 'We need to select a container orchestration platform capable of running stateful and stateless workloads with automated scaling and self-healing.', 'We need to define a comprehensive backup and disaster recovery strategy that meets our RTO and RPO targets across all tier-1 systems.'] 的对应 context sentence。保存每个文件。在 Baserow 中，创建一个名为 "ADR Decision Registry" 的数据库，包含一个名为 "ADR Registry" 的表（字段：ADR ID [primary text], Title [text], Status [single-select: Draft/Review/Approved/Implemented], Author [text], Reviewer [text], Created Date [date], Review Duration Days [number]）；将上述 ADR 作为 exactly 3 rows 导入，设置 Status 为 Review，Reviewer 来自 ['Emma Wilson', 'Frank Nguyen', 'Grace Patel']，Created Date 为 2025-06-10，Review Duration Days 为 0。在 OpenProject 项目 "DevOps Automation" 中，为每个 ADR 创建 exactly one Epic-type work package，subject 为 "Implement ADR-<NNN>: <Title>"，assignee 为 OpenProject Admin，priority 为 Normal，并在 description 中包含单行 "Linked ADR file: devops-configs/docs/adr/<filename>"。

**步骤：**

1. 在 code-server 中，打开 devops-configs 项目，并使用 File Explorer 创建文件夹 docs/adr/
2. 使用来自 ['005-secrets-management.md', '006-container-orchestration.md', '007-backup-strategy.md'] 的文件名创建 3 个 ADR markdown 文件，并用来自 ['Secrets Management Solution for Production Workloads', 'Container Orchestration Platform Selection', 'Backup and Disaster Recovery Strategy'] 的标题和来自 ['We need to centralize secret storage and rotation to eliminate plaintext credentials from source control and improve auditability.', 'We need to select a container orchestration platform capable of running stateful and stateless workloads with automated scaling and self-healing.', 'We need to define a comprehensive backup and disaster recovery strategy that meets our RTO and RPO targets across all tier-1 systems.'] 的 context 填充精确的五行结构；保存每个文件
3. 在 Baserow 中，创建数据库 "ADR Decision Registry" 和 "ADR Registry" 表，并使用指定 field types
4. 将 exactly 3 rows 插入 ADR Registry，每个 ADR 一行，Status=Review，reviewers 来自 ['Emma Wilson', 'Frank Nguyen', 'Grace Patel']，Created Date=2025-06-10，Review Duration Days=0
5. 在 OpenProject 项目 "DevOps Automation" 中，为每个 ADR 创建 exactly 3 个 Epic work packages——每个 ADR 一个——使用规定的 subject pattern、assignee OpenProject Admin、priority Normal，并在 description 中链接 ADR 文件名

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- openproject: admin / AdminPass123!
