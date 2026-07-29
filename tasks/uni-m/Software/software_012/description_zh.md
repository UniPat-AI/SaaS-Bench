**任务要求：**

为 todo-api 服务发布一个 API endpoint registry 和 deprecation tracker。在 code-server 中，打开 todo-api 项目，并使用 Search panel（Ctrl+Shift+F），将范围限定为 files="todo-api/**" 并启用 regex，查找每一个匹配模式 @(app|bp)\.route\( 的 Flask route registration；记录每个 endpoint 的 HTTP method、URL path、source file 和 line number。在 Baserow 中，创建一个名为 "TodoAPI Endpoint Governance" 的数据库，其中包含一个名为 "API Endpoint Registry" 的表（字段：Endpoint ID [primary text, formatted EP-<NNN>], Method [single-select: GET/POST/PUT/PATCH/DELETE], Path [text], Source File [text], Line Number [number], Version [single-select: v1/v2/v3], Status [single-select: Active/Deprecated/Removed], Deprecation Date [date, nullable]）。按照 Source File 字母顺序然后按 Line Number 升序的确定性顺序，为每一个发现的 route 插入 exactly one row，Endpoint IDs 分配为 EP-001, EP-002, ...；Version 根据 [["/api/v1/", "v1"], ["/api/v2/", "v2"], ["/api/v3/", "v3"], ["/health", "v1"]]（按 Path 键控）进行设置，所有行的 Status=Active，Deprecation Date=null。

**步骤：**

1. 在 code-server 中，打开 todo-api 项目；使用 Search panel，并启用 regex 和文件范围 "todo-api/**"，查找匹配 @(app|bp)\.route\( 的 route registrations；收集每个匹配项的 method、path、file、line number
2. 在 Baserow 中，创建数据库 "TodoAPI Endpoint Governance" 和 "API Endpoint Registry" 表；按规定的确定性顺序为每个 endpoint 插入一行，使用连续的 Endpoint IDs 以及来自 [["/api/v1/", "v1"], ["/api/v2/", "v2"], ["/api/v3/", "v3"], ["/health", "v1"]] 的 Version 分配

**登录凭据：**

- code-server: (no username) / 8a128206e2177bce1e48e565
- baserow: admin@example.com / Admin1234
- openproject: admin / AdminPass123!
