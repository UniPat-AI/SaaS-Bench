**任务要求：**

第 1 步 — 海报视觉识别：
仔细查看电影海报（`tasks/multi-m/inputs/watcharr_poster_381.jpg`）。仅根据海报的视觉元素识别影片标题和导演：标题文字、人物构图、场景意象、导演署名以及制片厂标识。不要使用任何外部元数据 —— 所有信息都必须从海报图片本身读取。请描述你观察到的具体视觉元素（例如人物姿态、场景构图、色彩调性、文字位置）以确认识别结果。

第 2 步 — Watcharr 观影记录：
在 Watcharr 中，搜索你识别出的影片。将其观影状态设为 "Watched"。给出 **8 out of 10** 的评分（10 分制）。用 **English** 写一段简短影评，长度 **50–100 words**，内容须对影片的叙事结构或视觉语言进行实质性分析。影评不能只是泛泛赞美 —— 它必须涉及某个具体的形式或主题元素（例如嵌套式梦境结构及其对观众定位感的影响，或电影摄影如何外化心理状态）。

不可接受："Great film with stunning visuals and an amazing story."
可接受："The nested dream architecture creates mutually contradicting timeframes, embedding viewer disorientation as a formal device rather than mere spectacle — each layer strips a rationalization until the emotional core is exposed."

第 3 步 — SiYuan 期集文档：
在 SiYuan 中，打开或创建笔记本 "播客脚本" (Podcast Scripts)。创建一个新文档，标题为 **"EP-42：盗梦空间 — 意识迷宫与叙事层叠"**（如果识别出的影片标题不同，则使用其英文对应标题）。文档必须包含且仅包含以下四个章节标题：

1. **节目引言 / Episode Introduction** — 至少 100 个字符的背景说明，介绍影片或其文化意义
2. **核心论点 / Core Arguments** — 至少 3 个不同的分析维度（例如：叙事结构、视觉象征、主题阐释、时间机制）
3. **代表性场景分析 / Representative Scene Analysis** — 至少对 2 个具体场景进行详细分析
4. **尾声推荐语 / Closing Recommendation** — 你的个人推荐与收束性想法

这四个标题都必须出现。每个部分都必须满足其最低内容要求。

第 4 步 — 导演作品集双向链接：
在 SiYuan 中创建或打开一个标题为 "导演作品集-诺兰" (Director Filmography - Christopher Nolan) 的文档。该文档中列出至少 3 部该导演的其他重要影片。建立从 EP-42 文档到导演作品集文档的 **双向链接**。如果导演作品集文档尚不存在，请先创建它，然后再创建链接 —— 不要写指向不存在文档的死链接。

**步骤：**
1. 查看电影海报（`tasks/multi-m/inputs/watcharr_poster_381.jpg`）；描述具体视觉元素；识别影片标题和导演。
2. 在 Watcharr 中找到已识别影片；将状态设为 Watched，评分 8/10；写一段英文影评（50–100 words），对叙事/视觉进行实质性分析。
3. 在 SiYuan 中，于笔记本 "播客脚本" 创建文档 "EP-42：盗梦空间 — 意识迷宫与叙事层叠"，并包含全部 4 个必需章节标题。
4. 创建或打开 "导演作品集-诺兰"，其中列出 ≥3 部 Nolan 影片。建立从 EP-42 到导演作品集的双向链接。

**输入文件：**
- **File 1:** `tasks/multi-m/inputs/watcharr_poster_381.jpg`
  - Type: image
  - Role: movie_poster_for_identification

**登录凭据：**

- watcharr: admin / mw-admin-123
- siyuan: accessAuthCode=siyuan6037
