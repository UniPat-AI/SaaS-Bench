**Task Requirements:**

A restaurant partner supplied `tasks/multi-m/inputs/recipya_recipe_545.jpg`. Inspect the photo without assuming the dish name. Identify the cooking style and the vegetables that are visibly central to the dish, then use those observations to choose or create the matching recipe and build a stock plan.

1. In Recipya, search using 1–3 keywords derived from the photo. Use a result only if at least four of the five visually central vegetables in the photo are present in its ingredient list and it has at least five ingredients and four cooking instructions. If no result qualifies, create a recipe whose name fits the dish and meets those same completeness requirements. The recipe must belong to `admin@recipya.com`.
2. In Grocy, create a recipe whose name exactly matches the chosen Recipya recipe. Find or create one direct Grocy product for each of the visually central vegetables present in the Recipya recipe, then link it to the recipe with a positive amount.
3. Upload the supplied photo as the image of that exact Grocy recipe. Do not attach a generic or different dish image.
4. For each linked visual-vegetable product, compare current Grocy stock with a target of 5 units. If stock is below 5, add exactly one shopping-list entry for the deficit (`5 - current stock`). Use the exact note `Bistrot Provençal — <Recipya recipe name>`. Do not add products already at or above 5 units, unrelated products, or duplicate entries.

The selected Recipya recipe is the upstream source of truth: if it does not satisfy the photo-derived ingredient test, the Grocy recipe and shopping plan are not valid.

**Input file:**

- `tasks/multi-m/inputs/recipya_recipe_545.jpg` — restaurant dish photo to inspect and upload to the target Grocy recipe

**Login Credentials:**

- grocy: admin / admin
- recipya: admin@recipya.com / mw-admin-123
