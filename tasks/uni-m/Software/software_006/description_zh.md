**任务要求：**

为三个服务建立一个 SLO registry 和 error-budget dashboard。在 Baserow 中，创建一个名为 "Platform SLO Registry" 的数据库，其中包含一个名为 "Service SLOs" 的表（字段：Service [primary text], SLO Type [single-select: Availability/Latency/ErrorRate], Target [number with 2 decimals], Current [number with 2 decimals], Budget Remaining [number with 2 decimals], Breaching [boolean]）。精确插入三行，每个服务各一行，服务名称为 ["payments-gateway", "auth-service", "inventory-api"]，并使用来自 [["Availability", 99.95, 99.88], ["Latency", 180.00, 165.40], ["ErrorRate", 0.50, 0.72]] 的相应 target 和 current 值；对于 Availability，Budget Remaining 计算为 Target - Current，对于 Latency 和 ErrorRate，Budget Remaining 计算为 Current - Target（四舍五入到 2 位小数）；当 Budget Remaining < 0 时将 Breaching 设为 true，否则设为 false。在 Metabase 中，为 Baserow Postgres database connection 触发一次 database schema sync，使新的 "Service SLOs" 表可见，然后创建一个名为 "Platform SLO Targets vs Current" 的 saved question，针对该 database 返回每行的 Service, SLO Type, Target, Current, Budget Remaining，以 table 显示，并保存到 collection "Platform Reliability"。创建一个名为 "Platform Error Budget Tracker" 的 Metabase dashboard，位于同一个 collection 中，并将该 question 作为 card 添加进去。在 OpenProject 项目 "Infrastructure Upgrade" 中，为每一行 Breaching=true 的记录创建一个 Bug work package，subject 为 "SLO breach: <Service> (<SLO Type>)"，priority 为 High，description 为 "Current=<Current>, Target=<Target>, Budget Remaining=<Budget Remaining>"。

**步骤：**

1. 在 Baserow 中，创建数据库 "Platform SLO Registry" 和表 "Service SLOs"，包含指定字段；从 ["payments-gateway", "auth-service", "inventory-api"] 以及 [["Availability", 99.95, 99.88], ["Latency", 180.00, 165.40], ["ErrorRate", 0.50, 0.72]] 插入 exactly three rows，并按定义的规则计算 Budget Remaining 和 Breaching
2. 在 Metabase 中，为 Baserow Postgres database 触发 database schema sync，使 "Service SLOs" 表可被发现；如有需要创建 collection "Platform Reliability"；保存一个 table question "Platform SLO Targets vs Current"，针对 Baserow 显示所有三行的 Service, SLO Type, Target, Current, Budget Remaining
3. 创建一个 Metabase dashboard "Platform Error Budget Tracker"，位于 "Platform Reliability" 中，并将 saved question 作为 card 添加
4. 在 OpenProject 项目 "Infrastructure Upgrade" 中，为每一行 Breaching=true 的记录创建一个 Bug work package，标题为 "SLO breach: <Service> (<SLO Type>)"，priority 为 High，使用所需的 description 格式

**登录凭据：**

- baserow: admin@example.com / Admin1234
- code-server: (no username) / 8a128206e2177bce1e48e565
- metabase: admin@metabase.local / mw-admin-123
- openproject: admin / AdminPass123!
