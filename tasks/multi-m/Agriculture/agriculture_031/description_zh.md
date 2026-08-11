**任务要求：**
分析所提供的菜品照片以识别菜品。使用推导出的关键词在 Recipya 中搜索并找到精确食谱。读取配料表以识别主要绿色蔬菜配料。在 Grocy 中检查该蔬菜的库存。然后，在 FarmOS 中找到该蔬菜最新的 Harvest log，并提取其 OMRI 认证编号。最后，返回 Grocy，定位该蔬菜的产品，并将 Recipya Recipe ID 和 FarmOS OMRI 认证编号都追加到 Grocy 产品描述中。

**步骤：**
1. 分析菜品照片并在 Recipya 中定位对应的食谱。
2. 识别主要绿色蔬菜作为关键配料，并在 Grocy 中检查其状态。
3. 在 FarmOS 中定位该蔬菜最新的 harvest log，并提取 OMRI cert number。
4. 使用提取出的 OMRI cert number 和 Recipya Recipe ID 更新 Grocy 产品描述。

**输入文件：**
- **File 1:** `tasks/multi-m/inputs/recipya_recipe_006.jpg`
  - Type: image/jpeg
  - Source app: recipya
  - Metadata:
    - name: Beef and Broccoli Stir-Fry
    - cuisine: Chinese

**登录凭据：**

- recipya: admin@recipya.com / mw-admin-123
- grocy: admin / admin
- farmos: admin / admin123456
