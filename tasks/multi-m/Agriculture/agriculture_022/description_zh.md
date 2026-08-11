**任务要求：**
在 Grocy 中，检索所有带有真实批次标识的产品列表 - 具体来说，就是 `stock` 表中 `stock_id` 不是自动生成占位符的条目（即 `stock_id` 不以 `x` 开头）。对于每一个这样的 `stock_id`，通过 FarmOS 的 JSON:API 端点 `/api/log/harvest` 进行查询，并检查是否存在任一 harvest log 的 `attributes.lot_number` 值与该 `stock_id` **完全相等**。如果某个 Grocy 产品的 `stock_id` 没有对应的 FarmOS harvest log，你必须执行两项操作：1）在 Grocy 产品 description 中添加备注 'DISCREPANCY: No matching FarmOS harvest log found'，以及 2）在产品名称后追加 '[REVIEW REQUIRED]'。不要修改那些 `stock_id` 在 FarmOS harvest log 中有匹配项的产品。

**步骤：**
1. 提取所有批次标识 - 即 Grocy 的 `stock` 表中 `stock_id` 不以 `x` 开头的行。
2. 对于每个 `stock_id`，在 FarmOS harvest logs 中搜索其 `lot_number` 属性完全等于该值的记录。
3. 找出那些 `stock_id` 在 FarmOS harvest logs 中缺失匹配的 Grocy 产品。
4. 通过在描述中追加差异备注并在名称后追加 `[REVIEW REQUIRED]` 来标记不一致的 Grocy 产品。

**登录凭据：**

- grocy: admin / admin
- farmos: admin / admin123456
