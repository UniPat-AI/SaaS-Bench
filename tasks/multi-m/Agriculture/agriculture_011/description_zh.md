**任务要求：**

步骤 1 - 顺序视觉分析：
先检查 `tasks/multi-m/inputs/farmos_crop_043.jpg`（整块田地概览）：注意整体冠层状况，以及从远处是否可检测到虫害。然后检查 `tasks/multi-m/inputs/farmos_crop_044.jpg`（穗部/叶鞘近景）：观察是否有密集的蚜虫群、蜕皮以及叶片损伤。结合两张图的观察结果，依据下表判断严重程度：

| 严重程度 | 视觉标准 | 干预措施 |
|----------|----------------|--------------|
| Low | 零散虫体，无聚集，无可见叶片损伤 | 仅监测，3 天后复查 |
| Medium | 局部聚集，轻微黄化或卷叶 | 使用 Neem Oil（OMRI-listed） |
| **High** | 穗部/叶鞘处有密集虫群，可见蜕皮 | 使用 Pyrethrin（OMRI-listed） |

步骤 2 - 在 FarmOS 中定位玉米植株资产：
找到现有的 corn / maize 植株资产。它可能显示为中文名称（例如 '玉米-大棚1号'）或英文名称（例如 'Corn Greenhouse 1'）。在继续之前，确认这两个名称指向同一个单一资产 - 不要创建重复项。

步骤 3 - 创建四条日志（所有字段内容必须为英文）：

**Log A - Emergency Observation Log（今天）：**
- Log type: Observation
- Asset: corn plant asset
- Attach `tasks/multi-m/inputs/farmos_crop_044.jpg` as the photo evidence
- In the notes field, record: (1) what tasks/multi-m/inputs/farmos_crop_043.jpg shows about overall canopy condition (note that it cannot confirm or rule out aphid density at distance), (2) what tasks/multi-m/inputs/farmos_crop_044.jpg shows about dense aphid clustering at the tassel/leaf sheath base (shed skins visible), (3) final severity determination: "High"
- Set a `severity` annotation to "High"

**Log B - Input Log（今天）：**
- Log type: Input
- Asset: corn plant asset (same as Log A)
- Notes must include: pesticide name "Pyrethrin (OMRI-listed)", application rate "200 mL/acre", organic certification number "OMRI-2023-PY-001", operator "Li Shifu", equipment "Power Sprayer No. 1"

**Log C - Follow-up Observation Log（今天 + 7 天）：**
- Log type: Observation
- Asset: corn plant asset (same as Log A)
- Date must be exactly 7 calendar days after today (handle cross-month arithmetic correctly, e.g. Jan 27 + 7 = Feb 3, not Jan 34)
- Notes must describe: aphid count reduced by approximately 70%, recommend continued monitoring for 7 more days before deciding on re-application

**Log D - Maintenance Log（今天）：**
- Log type: Maintenance
- Asset: **equipment asset** "Power Sprayer No. 1" (NOT the corn plant asset)
- Notes: post-spray equipment cleaning with water rinse to prevent organic pesticide cross-contamination

**步骤：**
1. 依次检查 tasks/multi-m/inputs/farmos_crop_043.jpg（整田）和 tasks/multi-m/inputs/farmos_crop_044.jpg（近景）；记录你的观察结果。
2. 在 FarmOS 中定位现有的 corn 植株资产（同时尝试中文和英文名称变体）。
3. 在 corn plant asset 上创建 Emergency Observation Log（今天）：附加 tasks/multi-m/inputs/farmos_crop_044.jpg，记录双图观察结果，注明严重程度 "High"。
4. 在 corn plant asset 上创建 Input Log（今天）：包含 Pyrethrin、200 mL/acre、cert# OMRI-2023-PY-001、operator Li Shifu、equipment Power Sprayer No. 1。
5. 在 corn plant asset 上创建 Follow-up Observation Log（今天 + 7 天）：约 70% 减少，建议继续监测。
6. 在 **equipment** 资产 "Power Sprayer No. 1" 上创建 Maintenance Log（今天）：喷洒后用清水冲洗设备。

**输入文件：**
- **File 1:** `tasks/multi-m/inputs/farmos_crop_043.jpg`
  - Type: image
  - Role: full_field_overview_corn
- **File 2:** `tasks/multi-m/inputs/farmos_crop_044.jpg`
  - Type: image
  - Role: close_up_aphid_infestation_tassel

**登录凭据：**

- farmos: admin / admin123456
