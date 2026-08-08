**Task Requirements:**

A customer supplied `tasks/multi-m/inputs/recipya_recipe_006.jpg`. Inspect the photo and infer the dish, cuisine, primary green vegetable, and primary protein. Do not rely on filename metadata as the answer. Build a traceability chain from that visual inference across Recipya, Grocy, and FarmOS.

1. Recipya has no matching recipe. Create one under `admin@recipya.com` using the conventional English dish name supported by the photo. Set the inferred cuisine, include quantified ingredient lines for the primary green vegetable and primary protein, add at least four cooking instructions, and upload the supplied photo as that exact recipe's image. Record its numeric recipe ID.
2. In Grocy, create the product whose name is exactly the primary green vegetable inferred from the photo and record a positive purchase so it has stock.
3. In FarmOS, find the exact, most recent harvest log for that vegetable's side-shoot harvest from North Field East Bed. Ensure that log is linked to the land asset named exactly `North Field — East Bed`, then append the organic certification number `OMRI-ORG-2024-1187` to that exact log's notes; do not annotate another harvest log or another land asset.
4. In the Grocy product description, preserve any existing text and append both exact lines:
   - `Recipya recipe ID: <numeric recipe ID>`
   - `OMRI certification: OMRI-ORG-2024-1187`

The Grocy traceability description is valid only when its recipe ID comes from the visually verified Recipya recipe and its certification is present in the exact FarmOS harvest log.

**Input file:**

- `tasks/multi-m/inputs/recipya_recipe_006.jpg` — customer dish photo to inspect and upload to the target Recipya recipe

**Login Credentials:**

- recipya: admin@recipya.com / mw-admin-123
- grocy: admin / admin
- farmos: admin / admin123456
