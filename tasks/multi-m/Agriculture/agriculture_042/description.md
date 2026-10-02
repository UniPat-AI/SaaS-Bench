**Task Requirements:**
Build a three-system traceability chain from a supplied field photo. One photo of the plowing operation is supplied with this task. Inspect it and classify it using exactly one label from this rubric:

| Label | Visual criterion |
|---|---|
| `Manual` | No aerial drone and no tractor or other self-propelled field machine is visible. |
| `Tractor-only` | A tractor or other self-propelled field machine is visible, but no aerial drone is visible. |
| `Drone-assisted` | An aerial drone is visibly part of the field operation. |

Use the label exactly as displayed above, including capitalization and hyphenation, wherever `<field_method>` appears below. Do not use a field-method label until you have examined the supplied photo.

In FarmOS, create one land asset named exactly `Vineyard Block 1`. Locate the existing Harvest log named exactly `Spring Plowing Complete`, link it to that asset, and attach the supplied photo to it as its only image.

In that same FarmOS record, set the notes to exactly these two lines and nothing else:

`TRACEABILITY BATCH: VINO-2025-081`

`FIELD METHOD: <field_method>`

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

The exact batch number, image-derived field method, product name, and FarmOS source must agree across all three systems.

**Steps:**
1. Inspect the supplied photo and classify it with the controlled rubric.
2. Create the `Vineyard Block 1` land asset, link the `Spring Plowing Complete` Harvest log to it, attach the supplied photo, and set the two traceability lines as its notes.
3. Create the exact Grocy product and description from the classified field-method label.
4. Create the exact e-label wine record with the same generated traceability values.

**Login Credentials:**

- farmos: admin / admin123456
- grocy: admin / admin
- e-label: Admin / Admin2024!Pass
