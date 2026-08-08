**Task Requirements:**

Build a FarmOS-to-Grocy organic input reconciliation for the `Certified Garlic Plot`.

1. In FarmOS, find or create the plant asset `Certified Garlic Plot` and the equipment asset `Backpack Sprayer #2`.
2. Create an Input log named exactly `Neem Oil Application — Certified Garlic Plot` on the plant asset. Date it today, link `Backpack Sprayer #2` as equipment, and record all of the following in the notes: `Neem Oil`, `OMRI-2024-NO-007`, `150 mL/acre`, `4 acres`, and `applied during cooler morning hours to avoid leaf burn`.
3. Create a Maintenance log named exactly `Post-Application Rinse — Backpack Sprayer #2` on the equipment asset. Date it today and no earlier than the Input log. Its notes must contain `triple-rinse clean with clean water`.
4. Derive the total treatment requirement in mL by multiplying the application rate in the FarmOS Input log by its treated acreage. In Grocy, find or create the product named exactly `Neem Oil Concentrate`, configure its stock quantity unit as milliliters, and inspect its current stock.
5. If Grocy stock is below the derived requirement, add exactly one shopping-list entry for `Neem Oil Concentrate` with amount `derived requirement - current stock` and exact note `FarmOS input log <numeric log ID> — Certified Garlic Plot`. If stock already meets the derived requirement, do not add such an entry. Do not create duplicates or use a different FarmOS log ID.

The Grocy action is conditional on the complete FarmOS application and maintenance chain; a shopping entry based on an incomplete or different log is invalid.

**Login Credentials:**

- farmos: admin / admin123456
- grocy: admin / admin
