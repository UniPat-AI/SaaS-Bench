**任务要求：**

步骤 1 - 视觉分析与关键词推导：
仔细检查菜品照片（`tasks/multi-m/inputs/recipya_recipe_545.jpg`）。根据可见食材（颜色、形状、质地）、烹饪方式、地区菜系线索识别菜品类型。推导 1-3 个文本搜索关键词，以便在食谱数据库中定位这道菜。不要随机猜测 - 应用烹饪领域知识（例如，层叠的彩色蔬菜、法式乡村风格 → 可考虑使用 "ratatouille" 作为搜索词）。

步骤 2 - Recipya 关键词搜索与匹配判断：
使用推导出的关键词搜索 Recipya。判断结果是否构成有效匹配：匹配要求与可见菜品内容的食材重合度 ≥70%（而不只是名称相似）。

- **如果找到匹配食谱：** 读取 Recipya 中完整的配料表及其数量。
- **如果没有找到有效匹配：** 至少从照片中直接识别出 5 种可见食材（并估计数量），然后在 Recipya 中创建新食谱（包括：名称、≥5 种配料、至少 4 个烹饪步骤）。

步骤 3 - 按配料检查 Grocy 库存：
对于步骤 2 中食谱里的每一种配料，检查 Grocy 当前库存：
- 库存充足（> 500 g 或 > 5 units）→ 标记为 "available"
- 库存不足或缺货 → 添加到 Grocy 购物清单，并在备注中包含：所需数量以及餐厅名称 "Bistrot Provençal"

步骤 4 - 创建 Grocy Recipe：
在 Grocy 中创建新的 Recipe 条目：
- Name: 与 Recipya 食谱名称相同（无论是匹配到的还是新建的）
- Ingredient list: 关联到 Grocy products（必须与 Recipya 食材列表匹配；需要语义匹配 - Recipya 中的 "Aubergine" 在 Grocy 中可能是 "Eggplant" 或 "茄子"）
- 将菜品照片（`tasks/multi-m/inputs/recipya_recipe_545.jpg`）作为 Recipe 的图片附件上传（不是作为 product image）

**步骤：**
1. 检查菜品照片（`tasks/multi-m/inputs/recipya_recipe_545.jpg`）；识别菜品类型并推导 1-3 个文本搜索关键词。
2. 使用关键词搜索 Recipya。判断是否存在匹配（≥70% 食材重合）。如果没有匹配，则创建一个新的 Recipya 食谱，包含 ≥5 种配料和 ≥4 个烹饪步骤。
3. 对确认的食谱中的每种配料，检查 Grocy 库存。将缺货物品添加到 Grocy 购物清单，并注明数量和 "Bistrot Provençal"。
4. 在 Grocy 中创建一个与 Recipya 食谱同名的 Recipe，完整配料列表关联到 Grocy products，并将菜品照片（`tasks/multi-m/inputs/recipya_recipe_545.jpg`）作为 recipe image 上传。

**输入文件：**
- **File 1:** `tasks/multi-m/inputs/recipya_recipe_545.jpg`
  - Type: image
  - Role: dish_photo_from_restaurant_partner

**登录凭据：**

- grocy: admin / admin
- recipya: admin@recipya.com / mw-admin-123
