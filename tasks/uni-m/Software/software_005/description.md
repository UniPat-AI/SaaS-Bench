**Task Requirements:**

Conduct a technical asset inventory and stale-dependency audit across the blog-engine and weather-dashboard projects. In code-server, open the integrated terminal and read blog-engine/package.json in blog-engine and weather-dashboard/package.json in weather-dashboard; extract every entry in the 'dependencies' section of each package.json (do NOT include devDependencies) with its exact pinned version string. Create a Baserow database named "Frontend Dependency Audit 2026" with a table "Dependency Inventory" containing fields Project (primary text), Dependency Name (text), Current Version (text), Manifest File (text), Captured At (date), Stale (boolean). Insert one row per extracted dependency with Captured At set to 2026-04-15; set Stale=true for every dependency whose major version is below 3 or that appears in the list ['express', 'ejs', 'react'], otherwise false. In Metabase, create a collection named "Frontend Audit Insights" and inside it save two questions against the Baserow Postgres database: (1) "Dependencies by Project" — a bar chart showing the count of dependencies grouped by Project; (2) "Stale vs Current" — a pie chart showing the count of rows grouped by Stale. Create a Metabase dashboard named "Frontend Dependency Health" inside "Frontend Audit Insights" and add both questions as cards. In OpenProject project "Marketing Website", create exactly one work package of type Task with subject "Upgrade stale dependencies: 2026-04-15", priority High, and a description that lists every stale dependency as "<Project> / <Dependency Name> @ <Current Version>" on separate lines.

**Steps:**

1. In code-server, open the terminal and cat blog-engine/package.json and weather-dashboard/package.json; record every entry in the 'dependencies' section (do NOT include devDependencies) with its exact version string.
2. In Baserow, create database "Frontend Dependency Audit 2026" and a table "Dependency Inventory" with fields Project, Dependency Name, Current Version, Manifest File, Captured At, Stale; insert one row per extracted dependency with Captured At=2026-04-15 and compute Stale per the rule.
3. In Metabase, create collection "Frontend Audit Insights"; add two saved questions — a bar chart "Dependencies by Project" (count grouped by Project) and a pie chart "Stale vs Current" (count grouped by Stale) — against the Baserow Postgres database.
4. Create a Metabase dashboard "Frontend Dependency Health" inside "Frontend Audit Insights" and add both saved questions as cards.
5. In OpenProject project "Marketing Website", create a single Task work package titled "Upgrade stale dependencies: 2026-04-15" with priority High; its description must list every stale dependency as "<Project> / <Dependency Name> @ <Current Version>" on separate lines.

**Login Credentials:**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- metabase: admin@metabase.local / mw-admin-123
- baserow postgres (for the Metabase data source): host `host.docker.internal`, port = Baserow web port + 15, database `baserow`, username `baserow`, password `kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc`
- openproject: admin / AdminPass123!
