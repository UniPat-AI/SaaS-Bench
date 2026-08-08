**Task Requirements:**
The receiving office kept a delivery manifest whose batch number should be the exact name of a FarmOS **Harvest** log. Reconcile all eight rows against FarmOS and flag only the unmatched Grocy products.

A batch is matched only when FarmOS contains a log with both `Type = Harvest` and a name that is character-for-character identical to the manifest batch number. A same-named Activity, Observation, Seeding, Transplanting, or Input log does not count.

The listed Grocy seed products have empty descriptions. For every unmatched row, set the product description to exactly `AUDIT FLAG: Missing FarmOS harvest log | batch=<batch number>`, replacing `<batch number>` with that row's full manifest value. Leave every matched product unchanged and preserve all eight manifest names and barcodes. Do not place this audit marker on any product outside the manifest.

**Delivery manifest:**

| Grocy product (exact name) | Barcode | Batch number |
|---|---|---|
| Green Leaf Lettuce | 0000651041025 | Crop Scouting Report |
| Whole Kernel Corn | 00016056 | Fall Harvest - Corn |
| Spring onion | 00001373 | Soybean Planting Activity |
| Spring onions | 00008761 | Corn Field Inspection - East Plot |
| Peach | 00002523 | Spring Plowing Complete |
| Large flat mushrooms | 00019170 | Wheat Harvest Report |
| British Cox Apples | 00035309 | Water Quality Sampling |
| Iceberg Lettuce | 00040617 | Fall Harvest - Soybeans |

**Steps:**
1. Open FarmOS Logs and filter to the Harvest type.
2. Compare every batch number against the Harvest log names using exact matching.
3. In Grocy, identify each product by both its exact name and barcode.
4. Set the exact batch-specific audit description only on unmatched products; leave matched products unchanged and do not flag unrelated products.

**Login Credentials:**

- grocy: admin / admin
- farmos: admin / admin123456
