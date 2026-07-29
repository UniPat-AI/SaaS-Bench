**任务要求：**

针对 todo-api 和 blog-engine 项目运行一次安全漏洞审计，关注依赖 CVEs。在 code-server 中，打开 File Explorer，进入每个项目，并在编辑器中打开 todo-api/requirements.txt 和 blog-engine/package.json；提取每个其 pinned version 恰好匹配以下脆弱条目之一的 dependency：[{"library": "Flask", "version": "2.0.1"}, {"library": "Jinja2", "version": "3.0.1"}, {"library": "SQLAlchemy", "version": "1.4.22"}, {"library": "requests", "version": "2.25.1"}, {"library": "express", "version": "4.17.1"}, {"library": "ejs", "version": "3.1.6"}, {"library": "marked", "version": "2.0.0"}, {"library": "lodash", "version": "4.17.20"}]。在 Baserow 中，创建一个名为 "Dependency Security Audit 2025Q1" 的数据库，并包含一个名为 "CVE Registry" 的表（字段：CVE ID [primary text], Project [single-select: todo-api/blog-engine], Library Name [text], Vulnerable Version [text], Fixed Version [text], CVSS Score [number with 1 decimal], Severity [single-select: Critical/High/Medium/Low], Discovered Date [date]）。

**步骤：**

1. 在 code-server 中，打开 todo-api/requirements.txt 和 blog-engine/package.json，并识别每个 pinned version 出现在 [{"library": "Flask", "version": "2.0.1"}, {"library": "Jinja2", "version": "3.0.1"}, {"library": "SQLAlchemy", "version": "1.4.22"}, {"library": "requests", "version": "2.25.1"}, {"library": "express", "version": "4.17.1"}, {"library": "ejs", "version": "3.1.6"}, {"library": "marked", "version": "2.0.0"}, {"library": "lodash", "version": "4.17.20"}] 中的每一个 dependency。
2. 在 Baserow 中，创建数据库 "Dependency Security Audit 2025Q1" 和表 "CVE Registry"，使用完全指定的 schema（字段：CVE ID [primary text], Project [single-select: todo-api/blog-engine], Library Name [text], Vulnerable Version [text], Fixed Version [text], CVSS Score [number with 1 decimal], Severity [single-select: Critical/High/Medium/Low], Discovered Date [date]）。

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
