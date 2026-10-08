**任务要求：**

步骤 1 - 在 e-label 中创建一条新的葡萄酒记录，并填写所有必需的合规字段：

| Field | Required value |
|-------|---------------|
| Producer | Farm/winery name as recorded in FarmOS (must match exactly) |
| Vintage | 2023 |
| AOC / Appellation | The certified organic production region for this farm |
| Grape Variety | Pinot Noir, 100% |
| Alcohol % | Must use format "13.5% vol" (not "13.5%" or "13.5度") |
| Net Volume | 750 mL |
| Allergens | Must include the text "Sulphites" or "亚硫酸盐" (exact substring) |

所有七个必填字段都必须非空。缺少任意一个都会使标签不合规。

步骤 2 - 使用侍酒师级别的领域推断填写面向消费者的感官字段：
基于声明的葡萄品种（Pinot Noir）及其已知特征，完成以下字段。取值必须基于 Pinot Noir 的真实特性 - 不能使用通用默认值：

- Serving temperature: Pinot Noir is a light-bodied red; correct range is 12–16°C (not the 8–10°C used for white wines)
- Glass type: Burgundy glass (not Bordeaux glass — Pinot Noir's aromatics require a wider bowl)
- Food pairings: at least 2 specific dish names appropriate for Pinot Noir (e.g. duck breast with cherry sauce, mushroom risotto, Burgundy-style beef)
- Tasting description: ≤100 characters; must mention at least one of: aroma profile, tannin level, acidity, or finish of Pinot Noir

步骤 3 - 导出 / 生成带二维码的数字标签预览：
保存记录后，触发二维码 PDF 导出。输出内容必须包含一个可正常扫描的二维码，嵌入在文档中 - 不是装饰性图形。

**步骤：**
1. 在 e-label 中创建一条新的葡萄酒记录。按指定填写全部 7 个必需合规字段。
2. 使用 Pinot Noir 的领域知识填写 serving temperature（12–16°C）、glass type（Burgundy）、food pairings（≥2 个具体菜品）以及 tasting description（≤100 chars，需提到 aroma/tannin/acidity/finish）。
3. 在保存前，验证 Producer 字段与 FarmOS 中已有的 farm/winery 名称完全一致。
4. 将 e-label 记录导出为带二维码的 PDF。

**登录凭据：**

- e-label: Admin / Admin2024!Pass
