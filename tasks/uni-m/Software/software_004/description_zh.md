**任务要求：**

规划一个同步的两周 sprint，名为 "Sprint Synchronize 2025-W10"，覆盖两个团队（Backend 和 Data），开始于 2025-03-03，结束于 2025-03-17。在 OpenProject 项目 "Data Analytics Pipeline" 中，创建一个名为 "Sprint Synchronize 2025-W10" 的 Version，并设置指定的开始和结束日期以及 status "open"。为每个团队创建 exactly 4 个 work packages（subject 以 "[Backend]" 或 "[Data]" 为前缀），类型均为 Feature，分配到 version "Sprint Synchronize 2025-W10"，并使 estimated time 值分别总和为 team A 的 32 和 team B 的 28。对于 team A 中 exactly one work package，添加一个 "follows" relation 指向 team B 中 exactly one work package，以建模跨团队依赖。在 code-server 中，打开 todo-api 项目，并在 todo-api/app.py 的顶部添加单行注释 "# Sprint Sprint Synchronize 2025-W10: integration touchpoint" 并保存。创建一个 Baserow 数据库 "Sprint Capacity Planner"，其中包含一个名为 "Sprint Capacity" 的表（字段：Team [primary text], Planned Hours [number], Work Package Count [number], Has Cross-Team Dep [boolean]），并添加 exactly two rows，反映 OpenProject 中的 counts 和 sums。

**步骤：**

1. 在 OpenProject "Data Analytics Pipeline" 中，创建 Version "Sprint Synchronize 2025-W10"，日期为 2025-03-03 到 2025-03-17
2. 为每个团队创建 4 个 Feature work packages，使用指定的 subject prefix 约定，全部分配到该 version，并设置 estimated hours，使其总和达到指定的团队总计
3. 在 team A 的一个 work package 和 team B 的一个 work package 之间添加 exactly one "follows" relation
4. 在 code-server 中，在 todo-api/app.py 顶部添加指定的注释行并保存
5. 在 Baserow 中，创建 "Sprint Capacity Planner" 和 "Sprint Capacity" 表，然后用聚合后的 planned hours 和 work package counts 填充两行

**登录凭据：**

- openproject: admin / AdminPass123!
- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
