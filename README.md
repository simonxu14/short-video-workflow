# short-video-workflow

> 一个面向 AI 短视频系列项目的**可执行 Skill**：从选题、脚本、AI 生成、后期，到多平台发布与数据回流的轻量流水线，只在两处停下等人确认，其余自动完成。

- **单文件守卫** `scripts/flow.py`：只拦真风险（模型被降级、重复生成/投稿、结果不明时重投、原创/AI 声明漏开、无证据发布、数据提前采集或补零），其余提示一律放行。
- 不绑定任何平台 SDK：生成调用你自己的视频生成工具（默认 Seedance 2.5），发布通过浏览器现场操作。
- 无定时任务、无后台服务：每次开工先跑 `metrics-due` 闭环往期数据，再做新一期。

## 目录结构

```
short-video-workflow/
├── SKILL.md              # Skill 入口（执行原则、硬规则、生成静默窗口）
├── VERSION               # 版本号
├── references/
│   ├── workflow.md       # 完整流程、flow.py 命令速查、中断恢复、平台坑点
│   └── craft.md          # 内容制作要点（选题/脚本/画面/自检）
├── scripts/
│   └── flow.py           # 唯一守卫脚本（零第三方依赖，Python 3.9+）
└── tests/
    ├── pytest.ini
    └── test_flow.py      # 守卫及恢复回归测试
```

## 适合做什么

- 每天/隔天产出一条 1 分钟以内的 AI 生成短视频，并发布到多个平台（如抖音、快手、B 站、小红书、视频号）。
- 需要严格记账：一期一平台一条作品、生成次数预算、发布必须截图留证、数据到期才采。
- 需要人只在关键节点拍板：**确认①** 选题+脚本+生成方案；**确认②** 成片+五平台文案。

不适合：单纯剪辑、与发布无关的一次性问答、需要平台开放 API 才能完成的自动化。

## 快速开始

```sh
# 1. 把 scripts/flow.py 拷到你的项目根目录
cp scripts/flow.py /path/to/your-series/
cd /path/to/your-series

# 2. 初始化本地配置（只补缺、不覆盖已有文件）
python3 -B flow.py init --series "我的短视频系列" --model seedance_2.5 --account "账号显示名"

# 3. 开工
python3 -B flow.py status        # 看进度
python3 -B flow.py metrics-due   # 先采到期的往期数据
python3 -B flow.py topics        # 题库去重 + 历史表现打分

# 原片到手：镜头切点 + 抽帧拼图（人工看伪品牌/伪文字）
python3 -B flow.py raw-check --raw raw.mp4 --day-dir workspace/2026-11-02
# 成片：EBU R128 响度 + 句界切点接近度（不证明语义对齐）
python3 -B flow.py master-check --master master.mp4 --timeline 逐句时间轴.json
```

完整命令与各阶段约束见 `references/workflow.md`。

v2.4 在v2.3防重复与版本批准基础上，增加同款旁白＋动作留白总预算、局部修改不变项与导出证据核验，以及交付/期限/归档分离。`timing-check`、`revision-check`、`delivery-next`都是只读辅助，详细合同见 `references/production-contracts.md`。MediaKit真实交付门禁保留，超时不默认选择、不改来源绕过；平台手册补充标签异步校验、窄视口/受控控件与实际封面核对。升级不追填旧视频、不重生成/重投。

v2.4.1只补两项：平台手册澄清视频/封面上传目标及B站真实提交按钮；`issue open/resolve`记录已发现问题，全端已发布但有open问题时返回`published_pending_cleanup`。不新增每平台必填审核材料，不自动查重/删作品，不改变旧投稿状态或防重投规则。

## 核心规则（不可放宽）

1. 只用配置里锁定的模型，禁止降级/替换；不超过单条时长上限就不拆段。
2. 生成或投稿结果不明：先核查原结果，**禁止重新生成/重新投稿**；一期一平台只允许一条作品。
3. 平台支持原创声明就必须开；AI 标识必须开。
4. 发布必须有截图证据；数据到期才采，看不到写缺失，**不补零、不编造**。
5. 登录、验证码、支付由人亲自完成。

## 配置文件

`flow.py init` 会在项目根生成：

- `video_production_policy.json`：模型、时长、画幅、生成次数预算等硬约束。
- `platform_rules.json`：各平台的原创声明/AI 标识现场文案（首次发布时人工读屏后填入）。
- `publish_pack.template.json`：发布包模板。
- 日常台账：`workspace/<日期>/`（计划、回执、成片、截图、发布状态）、`performance_history.jsonl`、`topic_bank.json`。

## 测试

```sh
python3 -m pytest -p no:cacheprovider -q tests
```

## License

[MIT](LICENSE)
