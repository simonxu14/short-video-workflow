# 制作与交付合同（v2.4）

## 目录

- 旁白＋动作留白预算
- 局部修改不变项
- 预览、链接选择与画布归档
- 检查命令的可信边界

## 旁白＋动作留白预算

确认①前实测同款TTS，预算总量不超过本期目标。源音轨内停顿已计入源时长，不重复相加；长镜动作可与旁白重叠，只把新增无旁白留白纳入预算。超时先精简口语稿，不自动提速；短于26秒并非失败，不为凑字破坏自然讲述。

当日平铺保存 `timing.json`（以下为示例，路径/音色/停顿必须换成真实本期值）：

```json
{
  "target_seconds": 30,
  "source_audio": "voiceover.mp3",
  "frozen_text": "voiceover.txt",
  "voice": "series-fixed-voice",
  "rate": "+40%",
  "lead_seconds": 0.5,
  "tail_seconds": 1.3,
  "added_pauses": [
    {"after_source_second": 15, "seconds": 3.2, "reason": "完整动作与反应留白"}
  ]
}
```

- `python3 -B flow.py timing-check --manifest timing.json`：ffprobe真实源音频，计算源时长＋新增停顿＋片首尾，返回剩余量和源音轨/冻结文本SHA256；不生成或修改音频。
- 生成计划填 `_timing_manifest: "timing.json"`，gen-check复核目标时长一致；新方案必须引用，旧计划不强制迁移。
- 旁白冻结后字幕程序化读取同一文本，ASR专名/同音误差只触发回听，不改冻结字幕。停顿可依实际画面校准，但正文/同款声线不变，更新时轴并再实测完整音轨。
- 最终语音结束、动作结果、字幕边界分别实测。不得用估算字数或字幕终点宣称声音完整。

## 局部修改不变项

修改前冻结：正文、字幕时轴、字幕样式配置/字体资产、同款旁白源、AI/栏目图层和完整动作区间。尽可能改干净画面底片后统一重渲染整套图层，避免插镜携带另一套字体、双重字幕或角标缺口；是否可这样处理受当前公开媒体工具能力约束。

工具不支持任意图像/可见水印叠加时，不把字幕工具冒充水印，也不调用无关生成工具；不因历史本地脚本存在就视为许可。选择当前已允许且明确支持的路径；没有可行路径则一次报告能力缺口和受影响项，不交付假装全通过的成片。

当日平铺保存 `revision-contract.json`，before/after都引用真实素材：

```json
{
  "before": {
    "master": "master-before.mp4",
    "frozen_text": "voiceover.txt",
    "subtitle_timeline": "subtitles.json",
    "subtitle_style": "subtitle-style.json",
    "voice_source": "voiceover.mp3",
    "frozen_hashes": {
      "frozen_text": "sha256:真实修改前正文指纹",
      "subtitle_timeline": "sha256:真实修改前时轴指纹",
      "subtitle_style": "sha256:真实修改前样式指纹",
      "voice_source": "sha256:真实修改前声音源指纹"
    },
    "layers": {
      "ai_label": {"asset": "ai-label.png", "sha256": "sha256:真实图层指纹", "coverage": [{"start": 0, "end": 30}]},
      "series_label": {"asset": "series-label.png", "sha256": "sha256:真实图层指纹", "coverage": [{"start": 0, "end": 30}]}
    }
  },
  "after": {
    "master": "master-after.mp4",
    "frozen_text": "voiceover.txt",
    "subtitle_timeline": "subtitles.json",
    "subtitle_style": "subtitle-style.json",
    "voice_source": "voiceover.mp3",
    "frozen_hashes": {
      "frozen_text": "sha256:真实修改前正文指纹",
      "subtitle_timeline": "sha256:真实修改前时轴指纹",
      "subtitle_style": "sha256:真实修改前样式指纹",
      "voice_source": "sha256:真实修改前声音源指纹"
    },
    "layers": {
      "ai_label": {"asset": "ai-label.png", "sha256": "sha256:真实图层指纹", "coverage": [{"start": 0, "end": 30}]},
      "series_label": {"asset": "series-label.png", "sha256": "sha256:真实图层指纹", "coverage": [{"start": 0, "end": 30}]}
    }
  },
  "export_review": {
    "master_sha256": "真实after母版SHA256（含sha256:前缀）",
    "subtitle_text_and_style": {"passed": true, "evidence": "revision-review.json"},
    "labels_visible": {"passed": true, "evidence": "revision-review.json"},
    "voice_complete": {"passed": true, "evidence": "revision-review.json"},
    "protected_actions": {"passed": true, "evidence": "revision-review.json"}
  }
}
```

- 修改前先记录不可变 `before.frozen_hashes`：frozen_text、subtitle_timeline、subtitle_style、voice_source分别对应真实 `sha256:` 指纹；before.layers每项记录素材 `sha256`。**不能只把前后指向同一个可覆盖文件**，否则同路径换稿也会漏检；旧源发生改变必须拒绝，不用事后重算指纹冒充修改前冻结。
- `subtitle-style.json`记录实际字体资产SHA256、字号、底板、颜色、位置；原配置与导出实际样式均核对，不能只填相同配置假称实际一致。
- `export_review`只在真实回听/抽帧/连续回看后填。证据报告记录方法、母版、插镜进入/内部/退出时间点、字幕/图层截图、音频回听和动作区间，不写伪造通过。
- `flow.py revision-check --manifest revision-contract.json`检查不变源文件哈希、实际成片规格/时长、覆盖区间及核验证据绑定；缺口/换字体/换稿/改时轴/缺回看证据拒绝。
- 局部修改发布包必须填 `_revision_manifest: "revision-contract.json"`；pack-check重检且绑定实际当前母版。旧成片无需追填；升级不重新处理旧视频。
- 编码转码后音轨字节不同不等于重新配音；保持同一声音源，实际回听完整和时轴无变化，不能声称码流逐字节一致。

## 预览、链接选择与画布归档

三类状态分开保存：`delivery_status`、`link_choice/choice_status`、`canvas_archive_status`；发布批准继续只放在publish_status，预览或期限选择不授权外发。

- 视频生成/内容编辑工具成功结果：按当前创作Skill先原始引用媒体卡直交，再做产后目录整理；不等QA/封面/文案/画布回读，不重复上传或create。
- 真正的本地正式导出：当前工具没有交付门禁时，以合法本地媒体卡立即预览；保留源环境ID。画布持久短链/归档另记，归档未完成不撤销已交付预览，不能假称归档完成。
- MediaKit返回 `mediakit_artifact_delivery`：必须先按真实agent_hint调用指定选择工具，原样参数，不用普通聊天/历史偏好代替。期限选择只属于交付环节，不是新增故事审核。
- 确认超时：记 `choice_status=timeout`，保留原任务ID/URL/原始hint及已落盘文件（仅当前规则允许时保存）；超时不等于拒绝/完成/未受理。不在同一次执行自动反复追问，不重跑媒体工具；一次说明具体阻塞，用户续跑时恢复同一选择。不能把MediaKit结果换成本地卡或改来源绕过门禁。
- keep_24h：实际确认后交付原始结果；transfer_7d：按MediaKit现行合同转存同一产物并交付，保留新地址和确认记录。路径可用但链接过期时只恢复原资源，无法恢复说明失败，不重新生成。
- 已有正式选择但渲染失败：只重交付同一资源，不再次询问期限。不把中间产物谎称为最终，也不把最终产物谎称“后续输入”规避选择。
- 图片/视频按当前上层媒体卡规则，文案/分镜/字幕等其他文件用present_files；本Skill不改变工具和平台权限。

只读辅助命令：`flow.py delivery-next --manifest delivery.json`。示例字段为origin（generation_tool/mediakit/local_export）、真实original_ref、delivery_gate（none/mediakit_link_choice）、真实agent_hint、choice_status、link_choice、choice_evidence、transferred_ref、delivery_status。原始hint存在不许改gate；超时无选择返回待恢复且无资源引用；已delivered只返回归档下一步，不重复交付。该命令不实际提交选择/上传/展示，不证明链接可访问。

## 检查命令的可信边界

三个命令仅本地只读：不会生成、编辑、发布或写用户状态，不自动修改历史。时长与规格来自探测；文件哈希/覆盖区间/证据绑定可以自动核验，视觉、语义、听感、工具返回来源真实性和用户确认真实性由执行者实际核实，不能凭成功JSON宣称全部完成。每次调用媒体/浏览器前重新加载其当前Skill合同。
