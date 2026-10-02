**Task Requirements:**

A customer supplied `tasks/multi-m/inputs/recipya_recipe_006.jpg`. Inspect the photo and infer the dish, cuisine, primary green vegetable, and primary protein. Do not rely on filename metadata as the answer. Build a traceability chain from that visual inference across Recipya, Grocy, and FarmOS.

1. In Recipya, ensure there is exactly one matching recipe under `admin@recipya.com`, creating it if necessary, using the conventional English dish name supported by the photo. Set the inferred cuisine, include quantified ingredient lines for the primary green vegetable and primary protein, add at least four cooking instructions, and upload the supplied photo file itself as that exact recipe's image. Recipya normally converts an uploaded image to its generated WebP representation; that application-side conversion is expected. The stored WebP must be the normal Recipya conversion of `tasks/multi-m/inputs/recipya_recipe_006.jpg`, not a crop, manual resize, re-export, or visually similar replacement. Record the recipe's numeric ID.
2. In Grocy, ensure there is exactly one product whose name is exactly the primary green vegetable inferred from the photo, creating it if necessary, and record a positive purchase so it has stock.
3. In FarmOS, use the one Harvest log named exactly `2024 Broccoli Harvest — North Field East Bed (Side Shoots)`. It must be linked only to the one land asset named exactly `North Field — East Bed`. Set that log's notes to exactly `OMRI certification: OMRI-ORG-2024-1187`. Do not put this certification on any other FarmOS log.
4. Set the Grocy product description to exactly these two lines in this order, with no other text:
   - `Recipya recipe ID: <numeric recipe ID>`
   - `OMRI certification: OMRI-ORG-2024-1187`

The Grocy traceability description is valid only when its recipe ID comes from the visually verified Recipya recipe and its certification is present in the exact FarmOS harvest log.

**Input file:**

- `tasks/multi-m/inputs/recipya_recipe_006.jpg` — customer dish photo to inspect and upload to the target Recipya recipe

**Login Credentials:**

- recipya: admin@recipya.com / mw-admin-123
- grocy: admin / admin
- farmos: admin / admin123456
