**Task Requirements:**

Inspect the two supplied corn images in sequence and use the visual evidence to choose the response. Do not assume a severity before examining both images.

| Severity | Visual criteria | Response | Follow-up |
|---|---|---|---|
| Low | Scattered insects, no clustering, no visible leaf damage | Monitor only; do not create a treatment Input log | Today + 3 days |
| Medium | Localised clusters with mild yellowing or leaf curl | `Neem Oil (OMRI-listed)`, `1.0 L/acre`, certification `OMRI-2024-NEEM-002` | Today + 5 days |
| High | Dense insect mass at the tassel or leaf-sheath base, with shed skins visible | `Pyrethrin (OMRI-listed)`, `200 mL/acre`, certification `OMRI-2023-PY-001` | Today + 7 days |

1. In FarmOS, use the existing plant asset `2023 Sweet Corn Planting 1`. Create one Observation log named exactly `AG011 - Corn Aphid Emergency Assessment`, dated today. Attach both supplied images to this log. In notes, separately describe what the field overview establishes, what the close-up establishes, and end with `Severity: <Low|Medium|High>` using your visual conclusion.
2. Apply the matching rubric response. For Medium or High, create one Input log named exactly `AG011 - Corn Aphid Treatment`, dated today, on the same corn asset. Include the exact treatment, rate, certification, operator `Li Shifu`, and equipment `Tractor-Mounted Boom Sprayer`.
3. Create one Observation log named exactly `AG011 - Corn Aphid Follow-up` on the same corn asset at the rubric's follow-up date. For the selected treatment response, record `Aphid count reduced by approximately 70%. Continue monitoring for 7 more days before deciding on re-application.`
4. If treatment was applied, create one Maintenance log named exactly `AG011 - Sprayer Decontamination`, dated today, on the existing equipment asset `Tractor-Mounted Boom Sprayer`. Record a post-spray water rinse to prevent organic pesticide cross-contamination.
5. In Grocy, ensure there is exactly one product with the exact selected treatment name, creating it if necessary. Add one shopping-list entry for that product with amount `1` and exact note `AG011 | FarmOS input #<input_log_id> | <exact treatment name>`, substituting the generated numeric FarmOS Input-log ID. Do not create an AG011-marked entry for an alternative treatment.

**Input files:**

- `tasks/multi-m/inputs/farmos_crop_043.jpg` — full-field overview
- `tasks/multi-m/inputs/farmos_crop_044.jpg` — tassel/leaf-sheath close-up

**Login Credentials:**

- farmos: admin / admin123456
- grocy: admin / admin
