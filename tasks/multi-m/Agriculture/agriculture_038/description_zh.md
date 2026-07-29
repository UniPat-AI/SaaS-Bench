**任务要求：**
遍历 Grocy 中所有在名为 'batch_number' 的 Product 自定义用户字段中分配了批次号的产品。对于每个批次号，查询 FarmOS 以确认是否存在一个 harvest log，其 'name' 与该批次号完全匹配。如果某个 Grocy 产品的批次号在 FarmOS 中没有匹配的 harvest log 名称，则在 Grocy 产品描述中追加 'DISCREPANCY: No FarmOS Harvest Log'。

**步骤：**
1. 检索当前在 Grocy 中生效的所有批次号。
2. 对每个批次号，在 FarmOS harvest logs 中搜索完全匹配项。
3. 找出批次号未匹配的产品。
4. 在未匹配的 Grocy 产品描述中追加差异文本。

**登录凭据：**

- grocy: admin / admin
- farmos: admin / admin123456
