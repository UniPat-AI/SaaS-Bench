**任务要求：**

对 data-analyzer 和 todo-api 项目执行一次测试执行审计。在 code-server 中，打开集成终端，进入每个项目目录，并运行项目的测试命令（data-analyzer 使用 `pytest tests/test_analyzer.py -v`，todo-api 使用 `make test`）。解析每次运行的输出并记录：tests passed、tests failed，以及 pass rate percentage（passed / (passed + failed) * 100，四舍五入到两位小数）。创建一个 Baserow 数据库 "Regression Test Audit March 2026"，其中包含一个名为 "Test Execution Audit" 的表，字段为 Project (primary text), Tests Passed (number), Tests Failed (number), Pass Rate (number with 2 decimals), Pass/Fail (single-select: Pass/Fail), Captured At (date)。添加 exactly two rows——每个项目一行——使用测得的计数；当 pass rate >= 85.00 时将 Pass/Fail 设为 Pass，否则设为 Fail。在 OpenProject 项目 "product-catalog" 中，创建一个 type 为 Task 的单个 work package，subject 为 "Test Execution Audit Report"，并在 description 中包含测得的 pass rates、passed/failed counts，以及每个项目相对于阈值 85.00 是通过还是未通过。

**步骤：**

1. 在 code-server terminal 中，cd 到 data-analyzer 并运行 `pytest tests/test_analyzer.py -v`，然后解析输出以提取 tests passed 和 tests failed 的数量，并计算 pass rate percentage
2. 对 todo-api 重复上述操作，使用 `make test`
3. 在 Baserow 中，创建 "Regression Test Audit March 2026" 和 "Test Execution Audit" schema（Project, Tests Passed, Tests Failed, Pass Rate, Pass/Fail, Captured At），并添加 exactly two rows，填入测得的计数、计算得到的 pass rates，以及根据 85.00 评估的 Pass/Fail
4. 在 OpenProject "product-catalog" 中，创建一个单独的 Task work package，subject 为 "Test Execution Audit Report"，description 列出这两个项目各自的 measured pass rate、passed/failed counts，以及是否相对于阈值 85.00 通过或未通过

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- openproject: admin / AdminPass123!
