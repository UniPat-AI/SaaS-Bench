**任务要求：**
在 FarmOS 中，针对 'Vineyard Block 1' 创建一条春季犁地的 activity log，上传提供的田间照片作为该日志的附件，并在备注中记录批次号 'VINO-2025-001'。在 Grocy 中，创建一个新的产品 'Organic Estate Wine 2025'，并将其批次号（在 description 或 custom field 中）精确设置为 'VINO-2025-001'。在 e-label 中，为 'Organic Estate Wine 2025' 起草一条新的葡萄酒记录，并将其批次号设置为 'VINO-2025-001'。该批次号必须在这三个系统中逐字逐字符保持完全一致。

**步骤：**
1. 在 FarmOS 中记录春季犁地活动，并附上批次号和田间照片作为附件。
2. 在 Grocy 中创建对应的葡萄酒产品，确保包含批次号。
3. 使用完全相同的批次号在 e-label 中起草合规标签。

**输入文件：**
- **File 1:** `tasks/multi-m/inputs/farmos_crop_021.jpg`
  - Type: image/jpeg
  - Source app: farmos
  - Metadata:
    - log_name: Spring Plowing Complete
    - asset_name: Vineyard Block 1
    - notes: Plowed 120 acres. Soil conditions excellent. Ready for planting.

**登录凭据：**

- farmos: admin / admin123456
- grocy: admin / admin
- e-label: Admin / Admin2024!Pass
