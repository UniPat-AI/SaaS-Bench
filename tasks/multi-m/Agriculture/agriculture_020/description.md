**Task Requirements:**

Reconcile the `Layered Zucchini Casserole` recipe in Recipya with Grocy stock and create an exact conditional shopping plan.

1. In Recipya, use the recipe named exactly `Layered Zucchini Casserole` owned by `admin@recipya.com`. If it is absent, create it. Its ingredient list must include all five main vegetables: zucchini, eggplant, onion, mushrooms, and fresh tomatoes.
2. In Grocy, locate the product representing each vegetable, accounting for the direct aliases courgette/zucchini and aubergine/eggplant. Processed tomato products, onion powder, mushroom soup, prepared dishes, and other substring-only matches do not count. If no direct product exists for a vegetable, create one using its canonical name: `Zucchini`, `Eggplant`, `Onion`, `Mushrooms`, or `Fresh Tomatoes`. If more than one direct product represents a vegetable, choose the one with the greatest current stock; break a tie by the lowest product ID.
3. Treat 5 units as the target stock. For each selected product below 5 units, compute the deficit `5 - current stock` and add exactly one shopping-list entry for that amount. Use the exact note `Bistrot Provençal menu expansion`.
4. Do not add selected products already at or above 5 units. Do not add unrelated products, alternate aliases, or duplicate entries with the task note. If none of the five selected products is deficient, the correct result is no shopping-list entry with the task note.

The complete Recipya recipe is the prerequisite for the Grocy plan. A shopping list created without the exact recipe and all five ingredients is invalid.

**Login Credentials:**

- recipya: admin@recipya.com / mw-admin-123
- grocy: admin / admin
