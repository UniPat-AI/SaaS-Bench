**Task Requirements:**
Audit this receiving manifest against the FarmOS Harvest logs. Each row identifies a real Grocy product by exact name and barcode and records the FarmOS log name claimed as its batch.

A row is matched only when a FarmOS log has `Type = Harvest` and its name is character-for-character identical to the batch number. A same-named log of another type is still unmatched.

The listed Grocy seed products have empty descriptions. For every unmatched row, set the description to exactly `DISCREPANCY: No FarmOS Harvest Log | batch=<batch number>` using that row's complete batch number. Leave matched products unchanged and preserve all eight manifest names and barcodes. Do not place this discrepancy marker on unrelated products.

**Receiving manifest:**

| Grocy product (exact name) | Barcode | Batch number |
|---|---|---|
| Welch's, freeze-dried apple slices | 0000790400004 | Cover Crop Seeding |
| Organic Apple Cider Vinegar | 0008295663764 | Equipment Maintenance Record |
| Diced In Tomato Juice | 00010894 | Water Quality Sampling |
| Carrot & coriandre soup | 00018210 | Fertilizer Application Log |
| Tomato & Gorgonzola pasta sauce | 00021036 | Soybean Planting Activity |
| M&S smoked tomato sauce | 00024815 | Fall Harvest - Corn |
| Apple, Coconut Water, Cucumber, Spinach | 00030014 | Spring Plowing Complete |
| pressed British pear and blackcurrant juice | 00050555 | Planting Date Record |

**Steps:**
1. Filter FarmOS Logs to Harvest and compare every manifest batch exactly.
2. Identify each Grocy target by exact name and barcode.
3. Set the exact batch-specific discrepancy description only for unmatched rows.
4. Confirm that matched products remain unchanged and unrelated products carry no task discrepancy marker.

**Login Credentials:**

- grocy: admin / admin
- farmos: admin / admin123456
