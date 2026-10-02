**Task Requirements:**

Build a traceable public e-label for the estate's 2024 Pinot Noir. The trace code must be derived from records that FarmOS generates; it is not supplied in this task.

1. In FarmOS, create exactly one Land asset named `AG003 - Pinot Noir Vineyard Lot`. Save it and record its generated numeric asset ID from the asset page URL.
2. On that asset, create exactly one Harvest log named `AG003 - 2024 Pinot Noir Harvest Intake`, dated today. Use the exact notes sentence `Harvested Pinot Noir grapes for the 2024 Burgundy lot.` Save the log and record its generated numeric log ID from the URL.
3. Compute the trace code `PN24-A<asset_id>-H<log_id>`, substituting the two decimal IDs without padding or spaces. For example, asset 12 and log 345 would produce `PN24-A12-H345`; those example IDs are not the answer.
4. In e-label, create one product named exactly `Estate Pinot Noir` with:
   - Net volume: `0.75` liters
   - Vintage: `2024`
   - Type: `Red`
   - Appellation: `Burgundy`
   - Alcohol: `13.5`
5. Open the product's Edit form. Set Brand to the computed trace code, set Food Business Operator Name to `Boutique Organic Farm`, and add only the predefined allergen ingredient `Sulphites`.
6. Open the product's public label page from Details. Confirm that it shows the exact product name, vintage, and `13.5 % vol.`. Do not create duplicate FarmOS logs or duplicate e-label products.

**Steps:**
1. Create the exact vineyard Land asset and Harvest log, then read both generated IDs.
2. Compute the trace code from those two FarmOS IDs.
3. Create and complete the exact e-label product, using the computed trace code as Brand.
4. Verify the generated public label page.

**Login Credentials:**

- farmos: admin / admin123456
- e-label: Admin / Admin2024!Pass
