# 短视频流水线（v2 轻量版）

一句话：**AI 一路做到底，只在两处停下等你；其余卡点只在真出事时出现。**

## 你只需要介入的地方

| 时机 | 你做什么 | 说明 |
|---|---|---|
| ✋ 确认① 方案 | 看选题+脚本+生成方案，说「做」 | 一次同意 = 允许用锁定模型生成**一次** |
| ✋ 确认② 成片 | 看成片，说「发」 | 一次同意 = 五个平台连续发布 |
| 偶发：登录 | 扫码 | 视频号/小红书常失效；发布前先做一轮登录预检 |
| 偶发：结果不明 | 帮忙看一眼后台 | 只核查原作品，绝不重投 |
| 偶发：要重生成 | 说「重做」 | 已生成过的候选再次生成必须你点头 |

其余全部自动：选题研究、写脚本、TTS 时长实测、史实自查、生成、原片 raw-check、配音字幕、声画对轴、成片自检、填写各平台表单、记账、截图存证、到期采数据。

## 每一期的步骤

```
0 开工   flow.py metrics-due → 有到期窗口先把往期数据采完；status 的 followups 顺手处理
1 选题   flow.py topics 看题库参考 + 当日联网找新题 → 选 1 个最抓马、主角大众熟知的题
2 脚本   按 references/craft.md 写：强钩子 + 单旁白 + 反转 + 主页导流；
         同款 TTS 实测旁白压到 26–27s；自查史实；指定每个画面镜头与封面帧
         ✋ 确认①（选题、脚本、画面方案、模型/时长一次给用户看）
3 生成   flow.py gen-check → flow.py gen-start → 单次提交并静默等待 → 保存回执 → flow.py gen-log
         下载原片后先 flow.py raw-check --raw 原片.mp4 --day-dir D（抽帧拼图人工看伪品牌/伪文字）
4 后期   配音（已定稿就不换）、逐句时间轴、字幕、响度；
         先连续回看叙事因果/完整动作，确认错位后局部修复，禁止为切点报警碎切；
         flow.py master-check --master M --timeline 时间轴.json → ASR 回听 + 抽帧自检
         ✋ 确认②（成片 + 各平台标题文案一起给用户看）
5 发布   flow.py pack-check --approved "原话"
         每个平台：现场准备原表单 → checkpoint prepared（截图）→ start（点击提交前）→ 原表单提交一次 → 截图 → done
6 收尾   审核中的平台只复查原作品：done published（有可靠发布时间才建24h/48h/7d窗口）；resume确认completed
         <项目>/PROJECT_STATE.md 追加一条
```

## flow.py 命令速查（`python3 -B flow.py <命令> -h` 看参数）

在项目根或任意目录运行均可（自动找项目根，也可设 `FLOW_ROOT`）。查询类命令只读；写入类命令同一时间只允许一个在跑。

| 命令 | 作用 | 会拦什么 |
|---|---|---|
| `gen-check --plan P` | 生成前只读检查 | 模型/拆段/画幅/批准不合规；原请求仍在提交、等待或结果未知；已成功但未批准重做 |
| `gen-start --plan P [--regen-approved "原话"]` | 提交前原子检查并写 `submitting` 本地占位（不代表受理） | 重复占位、等待中/未知状态不能靠重做批准放行 |
| `gen-log --plan P --status submitted\|succeeded\|failed\|unknown [--evidence E] [--url U]` | 给原请求记回执/终态；E 相对项目根 | 终态缺非空证据；成功缺结果地址；未知回退为 submitted |
| `raw-check --raw R [--day-dir D] [--frames 12]` | 原片规格核验 + 镜头切点表 + 均匀抽帧拼图（写 `_raw_check/`） | 画幅错、时长超单条上限；**拼图必须人工看伪品牌/伪文字** |
| `master-check --master M [--timeline T] [--action-ranges A]` | 技术实测+句界切点接近度，完整动作区间多句不强切 | 规格/响度真风险；不判断语义，不强制逐句切镜 |
| `pack-check --day-dir D [--approved "发布原话"] [--platforms X ...]` | 检查发布包、绑定当前版本/范围批准 | 内容变化失效旧批准；已有原表单/投稿禁止换包覆盖 |
| `checkpoint --day-dir D --platform X --stage prepared\|waiting_auth\|ready --evidence E` | 保存准备/验证现场检查点，不计新投稿 | 无截图；未知投稿不得改PREPARED解锁 |
| `resume --day-dir D [--platform X]` | 只读给出原编辑页/验证/原结果/完成的下一步 | 不自动重传/重投 |
| `start --day-dir D --platform X` | 提交前原子占位，后仅提交原表单一次 | 缺当前版本/范围批准；待验证；已有/未知投稿；跨日重复 |
| `done --day-dir D --platform X --result submitted\|under_review\|published\|rejected\|unknown --evidence E [--published-at ISO]` | 原请求推进，核验时间与发布时间分开 | 无证据；已发布回退；已有受理不能退回未提交 |
| `metrics-due` | 列出到期待采的数据窗口 | — |
| `metric-add --day-dir D --platform X --window w24\|w48\|w7d --evidence 截图 --plays N ...` | 记数据并自动追加历史 | 没到期 / 重复采 / 全空补零 |
| `status [--day-dir D]` | 状态总览 + `followups`（审核中超 48h、确认①方案超 7 天没下文） | — |
| `topics` | 题库去重（跳过已做/被否）+ 历史表现打分（样本 n<3 不参与加权） | — |

计划 JSON 最少需要：`candidate_id`、`prompt`、`locked_parameters`（model_version / target_duration_seconds / aspect_ratio / video_count=1 / segments_seconds=[29] / fallback_allowed=false）、`user_approval.quote`（用户同意原话）。

## 生成等待与中断恢复

1. **先收齐辅助结果**：研究、审校、文案等任务完成后，再交给唯一生成执行者；提交前一次交齐批准方案和约束。不要在生成等待期间启动会向该执行者推送完成消息的任务。
2. **先占位，后单次提交**：`gen-check` 后执行 `gen-start --plan P`，成功才调用生成工具一次。`submitting` 仅表示本地开始尝试，不表示服务端受理；进程退出、会话中断或时间经过都不会自动解锁。不要改候选编号、复制计划或换执行者规避它。
3. **静默等待**：从提交到回执保存完成，不给执行者发进度、文案、催办或审校消息，不主动终止/重启它。例行汇报放在提交前或落盘后；用户主动叫停和真实安全风险仍须立即处理。此规则约束协作方式，不是消息系统的技术屏蔽。
4. **先保存，再通知**：将真实工具回执保存到当日目录，保留原始状态、实际返回的任务凭据/结果地址/错误文本；不存在的字段留缺失，不保存账号凭证。用 `gen-log --evidence E` 绑定非空回执及校验值；成功时另带 `--url U`，立即下载并核验原片，再进入后期。异步返回只查该原任务；下载失败只重取原资源，不重生成。
5. **中断先冻结**：超时、消息中断、无有效回执记 `unknown`；`submitting`、`submitted`、`unknown` 均禁止再次提交，`--regen-approved` 也不能越过。按原调用、原回执、正式任务历史核查，找到原片即接续后期。仅有明确失败/未受理证据才可附证据记 `failed`；找不到链接不是失败证据。更正只追加、不抹历史，也不把本地时间当成服务端时间。
6. **如实结束恢复**：已查入口、原始错误、仍缺的凭据分别说明；没有正式查询能力就明确限制，不猜接口、不反复提交。原请求终态核清前保持冻结。守卫只校验留痕与状态，证据真实性仍需现场核对；本次未知结果不会因升级而解除。

## 声画与完整动作核验

- `master-check --timeline T [--action-ranges A]` 统计句界与切点接近度，不验证语义；`cut_proximity_ok=true` 不能写成“声画已对齐”。
- `A` 为实测完整动作区间 `[{"start":12.708,"end":21.417}]`；内部可容纳多句旁白，不要求切镜。内部检出切点仅提示连续回看，不自动修剪。
- 连续回看消息→反应→行动→结果；核验人物/道具状态不倒退、每镜新增信息、高潮完整。只修实际确认的错位，禁止为消警重排/循环/碎切。

## 发布断点、时间与版本绑定（v2.3）

### 命令及恢复动作

- `checkpoint --day-dir D --platform X --stage prepared --evidence E --editor-url HTTPS`：现场确认表单、账号、当前视频、封面、文案、声明与可见范围；记PREPARED，不增加attempts。保存非认证编辑页地址，不存含凭据链接。
- 登录/验证码交接：按浏览器Skill完成必要交接，随后 `checkpoint --stage waiting_auth --evidence E`；返回后重新观察，通过才记 `--stage ready`。此检查点不覆盖投稿状态，也不解锁SUBMITTING/SUBMITTED/UNKNOWN。
- 只在原表单已准备、授权与必要交接都完成后执行 `start`，本地占位成功后点击提交**一次**；受理回执用 `done --result submitted --evidence E`，审核/发布终态再据实推进。仅本地占位不证明服务端受理。
- `resume --day-dir D [--platform X]` 只读返回每平台state、checkpoint、attempts、editor_url和next；不重新上传，不重新start。PREPARED先观察原编辑页再提交；SUBMITTING/SUBMITTED/UNKNOWN只查原结果；UNDER_REVIEW查原作品；PUBLISHED返回completed。
- 旧SUBMITTING/UNKNOWN不自动迁成PREPARED；明确证据证明未提交才可 `done --result not_submitted --evidence E --note 依据`，不能凭超时解锁。已有受理/审核记录禁止退回未提交。已发布不可回退。

### 发布批准与内容版本

- `pack-check --approved "明确发布原话" [--platforms douyin ...]` 将publication批准绑定真实母版SHA256、封面、账号、该范围各平台文案和范围本身；省略范围=当前发布包全部平台。内部 `_` 前缀备注不参与指纹。
- 用户只是认可局部修改方向：用 `--approval-kind revision` 留档，不授予新发布批准。代码不识别自然语言语义，执行者必须按真实用户意图选kind/range，不把“认可”强填publication。
- 未开始原表单/投稿时，母版/文案/封面/账号/范围变化使旧批准归档失效；更新发布包并重新交付当前版本后，才可登记本次明确发布批准。
- 已PREPARED/SUBMITTING/SUBMITTED/UNKNOWN/UNDER_REVIEW/PUBLISHED时不得改发布包覆盖原绑定；取消未提交准备需现场证据，已投稿只核查原作品，已发布不因换版重新放行。
- 旧批准没有版本指纹：作为历史保留，不自动补造成有效新授权；需要新外发时重新取得当前版本批准。升级不修改既有发布状态/次数/证据。
- 全部PUBLISHED时 `pack-check/status/resume` 返回completed，不再建议start。

### 可靠发布时间

- `done --result published --evidence E` 可记录后台已发布，但发布时间缺失时保留null、不建窗口；`verified_at`仅为本次核验记录时间。
- 有可靠时间时补 `--published-at ISO8601带时区 --time-source platform_backend|public_page|verified_receipt --time-precision minute|second|unknown --timezone-basis "核定依据"`。
- 后台只到分钟时00秒是规范化值，不宣称秒级精度；时区未核定不填写时间。复查不改旧发布时间，v1状态转换事件的at也不当发布时间。
- 已采数据后改时间须先审计纠错，不直接覆盖窗口。`metric-add`在发布时间未知时明确拒绝，不补零、不造窗口。

## 只在真风险处拦截（这些永不放宽）

- 只用 `video_production_policy.json` 指定的模型（如 seedance_2.5），不降级、不替换，≤配置单条时长上限不拆段。
- 结果不明（生成或投稿）先核查，**禁止重新生成/重新投稿**；一期一平台只允许一条作品。
- 平台支持原创声明就必须开；AI 标识必须开。
- 发布必须有截图证据；数据到期才采、看不到就写缺失、**不补零、不编造**。
- 登录、验证码、支付由用户亲自完成。

## 平台现场要点

完整实测手册（各平台声明位置、封面入口、时区口径、绕坑办法、视频号 shadowRoot 上传代码）见 **[platforms.md](platforms.md)**。要点：

- 小红书标题 ≤20 字；快手话题 ≤4 个。
- 抖音、快手、视频号后台时间跟随运行设备时区；B站、小红书是北京时间。拿不准时区就不写 `published_at`。
- 视频号上传用 CDP `DOM.setFileInputFiles` 注入 shadowRoot 内的 input，不点上传框。
- 视频号「原创审核中」是原创标记审核，复查到「已声明原创」再改 PUBLISHED。
