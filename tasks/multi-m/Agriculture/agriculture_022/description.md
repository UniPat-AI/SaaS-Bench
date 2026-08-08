**Task Requirements:**
The organic audit manifest below maps real Grocy products to FarmOS batch names. Reconcile all eight rows against FarmOS and mark only the rows that have no exact **Harvest** log match.

A batch is matched only when FarmOS contains a log with `Type = Harvest` whose name is character-for-character identical to the batch number. Logs of every other type are non-matches even when their names are identical.

The listed Grocy seed products have empty descriptions. For every unmatched row, make both changes below:

- Rename the product to `<original exact name> [REVIEW REQUIRED]` with one terminal suffix.
- Set its description to exactly `DISCREPANCY: No matching FarmOS harvest log found | batch=<batch number>` using that row's full batch number.

Leave matched products unchanged and preserve all eight manifest barcodes. Do not place the review suffix or discrepancy marker on any product outside this manifest.

**Delivery manifest:**

| Grocy product (exact original name) | Barcode | Batch number |
|---|---|---|
| Caillé nature | 0002000014391 | Soybean Planting Activity |
| Macarroni and cheese dinner | 0005329003107 | Irrigation System Check |
| Hard Boiled Eggs | 00003100 | Cover Crop Seeding |
| Creamed Honey, Multi-Floral & Clover Blossoms | 00015318 | Crop Scouting Report |
| mostly mesquite honey | 00015349 | Hay Baling Operation |
| West country luxury yogurt Rhubarb Custard | 00033893 | Spring Plowing Complete |
| Victoria plum and bergamot yogurt | 00033909 | Bird Netting Installation |
| Cornish Cove Grated Cheddar Mature | 00046473 | Grain Bin Inventory Check |

**Steps:**
1. Filter FarmOS Logs to Harvest and compare all batch names exactly.
2. In Grocy, identify each product by its exact original name and barcode.
3. Apply the exact name suffix and batch-specific description only to unmatched rows.
4. Verify that matched products remain unchanged and that unrelated products carry no task review signal.

**Login Credentials:**

- grocy: admin / admin
- farmos: admin / admin123456
