**Task Requirements:**
Build a three-system traceability chain from an existing FarmOS image record. FarmOS contains two Harvest logs named exactly `Spring Plowing Complete` on the asset `Vineyard Block 1`. Open both records and inspect their attached photos. Select the record whose photo shows **a farmer holding a tablet, a drone in the air, and a tractor beside a barn**; do not use the record whose photo shows empty field crates.

In the selected FarmOS record, preserve the existing notes and append both exact lines:

`TRACEABILITY BATCH: VINO-2025-081`

`FIELD METHOD: Drone-assisted`

Do not modify the notes of the other `Spring Plowing Complete` record.

In Grocy, create exactly one product named `Drone-Assisted Estate Wine 2025`. Set its description to the following three lines:

`TRACEABILITY BATCH: VINO-2025-081`

`FIELD METHOD: Drone-assisted`

`FARMOS SOURCE: Spring Plowing Complete | Vineyard Block 1`

In e-label, create exactly one wine record with these values:

- Name: `Drone-Assisted Estate Wine 2025`
- Brand: `Drone-Assisted Estate`
- SKU / batch number: `VINO-2025-081`
- Vintage: `2025`
- Additional information: `FIELD METHOD: Drone-assisted; FARMOS SOURCE: Spring Plowing Complete; Vineyard Block 1`

The exact batch number, image-derived field method, product name, and FarmOS source must agree across all three systems.

**Steps:**
1. Use the FarmOS attachments to identify the correct source record by visual content.
2. Append the two traceability lines to that record without removing its existing notes or changing the other candidate record.
3. Create the exact Grocy product and description.
4. Create the exact e-label wine record and complete the required traceability fields.

**Login Credentials:**

- farmos: admin / admin123456
- grocy: admin / admin
- e-label: Admin / Admin2024!Pass
