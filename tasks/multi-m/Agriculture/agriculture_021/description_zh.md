**任务要求：**
遍历 Grocy 中所有在其 'batch_number' 自定义用户字段中分配了批次号的产品。对于每个批次号，查询 FarmOS 中是否存在对应的 harvest log，且该 harvest log 的 name 与批次号完全相同。如果某个 Grocy 产品的批次号在 FarmOS 中没有匹配项，则编辑该 Grocy 产品，在其 description 字段中追加 'AUDIT FLAG: Missing FarmOS harvest log'。

**步骤：**
1. 从 Grocy 产品的自定义用户字段中检索所有批次号。
2. 将每个批次号与 FarmOS harvest log 名称进行交叉比对。
3. 对于任何未匹配的批次，在 Grocy 产品描述中追加差异标记。

**登录凭据：**

- grocy: admin / admin
- farmos: admin / admin123456
