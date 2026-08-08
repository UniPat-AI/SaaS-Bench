**Task Requirements:**

Tomatoes are overstocked in Grocy. Use a tomato-forward recipe from Recipya to build an exact restocking plan in Grocy. The recipe selection must use both the actual cover image and the ingredient list.

1. In Recipya, browse recipes owned by `admin@recipya.com` that have cover images. Select one recipe whose cover visibly presents tomato as the dominant ingredient or sauce base and whose ingredient list explicitly contains tomato or tomatoes. Do not select from the title alone. Record the recipe's numeric ID from its URL and its exact displayed name.
2. In Grocy, compare every non-tomato ingredient in that recipe with the current positive stock. A product counts as stocked only when it can directly supply that culinary ingredient; a flavored or composite product that merely mentions the ingredient does not count. Ignore quantities and preparation words, and deduplicate repeated forms of the same ingredient.
3. For each unique auxiliary ingredient that is not in positive stock, add exactly one Grocy shopping-list entry with amount `1`. Use an existing suitable Grocy product when available. If no suitable product exists, first create a product named exactly as the ingredient text displayed by Recipya, then add it.
4. Every entry created for this task must use the exact note `AG007 | Recipya #<id> | <exact recipe name>`, substituting the selected recipe's generated numeric ID and exact displayed name. Use the same note on every task entry.
5. Do not add tomato ingredients, already-stocked ingredients, unrelated extras, or duplicate entries. Do not modify stock amounts.

**Steps:**

1. Inspect Recipya recipe covers and ingredient lists, then choose a visually tomato-dominant recipe.
2. Read its generated recipe ID and exact name.
3. Compare all auxiliary ingredients against Grocy's positive stock.
4. Add the exact missing set to the Grocy shopping list with the generated Recipya marker.

**Login Credentials:**

- grocy: admin / admin
- recipya: admin@recipya.com / mw-admin-123
