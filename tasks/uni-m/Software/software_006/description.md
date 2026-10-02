**Task Requirements:**

Establish an SLO registry and error-budget dashboard for three services. In Baserow, create a database named "Platform SLO Registry" with a table "Service SLOs" (fields: Service [primary text], SLO Type [single-select: Availability/Latency/ErrorRate], Target [number with 2 decimals], Current [number with 2 decimals], Budget Remaining [number with 2 decimals], Breaching [boolean]). Insert exactly three rows, one per service in ["payments-gateway", "auth-service", "inventory-api"], using the corresponding target and current values from [["Availability", 99.95, 99.88], ["Latency", 180.00, 165.40], ["ErrorRate", 0.50, 0.72]]; compute Budget Remaining as Target - Current for Availability, and Current - Target for Latency and ErrorRate (rounded to 2 decimals); set Breaching=true when Budget Remaining < 0, else false. In Metabase, add the Baserow Postgres database as a new PostgreSQL data source named "Baserow Postgres" (Admin → Databases; connection details under Login Credentials) if it is not already present, then trigger a database schema sync so the new "Service SLOs" table is visible, then create a saved question named "Platform SLO Targets vs Current" against that database that returns Service, SLO Type, Target, Current, Budget Remaining for every row, displayed as a table, saved in collection "Platform Reliability". Create a Metabase dashboard "Platform Error Budget Tracker" in the same collection and add this question as a card. In OpenProject project "Infrastructure Upgrade", create one Bug work package per row where Breaching=true, with subject "SLO breach: <Service> (<SLO Type>)", priority High, and description "Current=<Current>, Target=<Target>, Budget Remaining=<Budget Remaining>".

**Steps:**

1. In Baserow, create database "Platform SLO Registry" and table "Service SLOs" with the specified fields; insert exactly three rows from ["payments-gateway", "auth-service", "inventory-api"] and [["Availability", 99.95, 99.88], ["Latency", 180.00, 165.40], ["ErrorRate", 0.50, 0.72]] computing Budget Remaining and Breaching per the defined rules.
2. In Metabase, add the Baserow Postgres database as a new PostgreSQL data source named "Baserow Postgres" (Admin → Databases; connection details under Login Credentials) if it is not already present, then trigger a database schema sync so the "Service SLOs" table is discoverable; create collection "Platform Reliability" if missing; save a table question "Platform SLO Targets vs Current" against Baserow showing Service, SLO Type, Target, Current, Budget Remaining for all three rows.
3. Create a Metabase dashboard "Platform Error Budget Tracker" in "Platform Reliability" and add the saved question as a card.
4. In OpenProject project "Infrastructure Upgrade", create one Bug work package for every Breaching=true row, titled "SLO breach: <Service> (<SLO Type>)", priority High, with the required description format.

**Login Credentials:**

- baserow: admin@example.com / Admin1234
- code-server: (no username) / 8a128206e2177bce1e48e565
- metabase: admin@metabase.local / mw-admin-123
- baserow postgres (for the Metabase data source): host `host.docker.internal`, port = Baserow web port + 27, database `baserow`, username `baserow`, password `kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc`
- openproject: admin / AdminPass123!
