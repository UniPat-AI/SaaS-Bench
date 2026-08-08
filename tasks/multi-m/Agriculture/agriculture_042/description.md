**Task Requirements:**
Build a three-system traceability chain from an existing FarmOS image record. FarmOS contains two Harvest logs named exactly `Spring Plowing Complete` on the asset `Vineyard Block 1`. Open both records and inspect their attached photos. Classify each photo using exactly one label from this rubric:

| Label | Visual criterion |
|---|---|
| `Manual` | No aerial drone and no tractor or other self-propelled field machine is visible. |
| `Tractor-only` | A tractor or other self-propelled field machine is visible, but no aerial drone is visible. |
| `Drone-assisted` | An aerial drone is visibly part of the field operation. |

Rank the labels `Manual < Tractor-only < Drone-assisted`. Select the single record whose photo receives the higher-ranked label. If both photos receive the same label, do not modify either record. Use the selected label exactly as displayed above, including capitalization and hyphenation, wherever `<field_method>` appears below.

In the selected FarmOS record, preserve the existing notes and append both exact lines:

`TRACEABILITY BATCH: VINO-2025-081`

`FIELD METHOD: <field_method>`

Do not modify the notes of the other `Spring Plowing Complete` record.

In Grocy, create exactly one product named `<field_method> Estate Wine 2025`. Set its description to the following three lines:

`TRACEABILITY BATCH: VINO-2025-081`

`FIELD METHOD: <field_method>`

`FARMOS SOURCE: Spring Plowing Complete | Vineyard Block 1`

In e-label, create exactly one wine record with these values:

- Name: `<field_method> Estate Wine 2025`
- Brand: `<field_method> Estate`
- SKU / batch number: `VINO-2025-081`
- Vintage: `2025`
- Additional information: `FIELD METHOD: <field_method>; FARMOS SOURCE: Spring Plowing Complete; Vineyard Block 1`

The exact batch number, image-derived field method, product name, and FarmOS source must agree across all three systems. Do not use a field-method label until both photos have been inspected and classified.

**Steps:**
1. Inspect both FarmOS attachments and classify each photo with the controlled rubric.
2. Select the uniquely higher-ranked record and append the two traceability lines without removing its existing notes or changing the other candidate record.
3. Create the exact Grocy product and description from the selected field-method label.
4. Create the exact e-label wine record with the same generated traceability values.

**Login Credentials:**

- farmos: admin / admin123456
- grocy: admin / admin
- e-label: Admin / Admin2024!Pass
