# 更新日志

## v0.2.2 (2026-10-01)

**需求**：主动发送不再要求逐会话显式开启——用户实测没人愿意在每个群里发 `/表情包 主动 开启`，配置留空却得到「未开启」的提示，语义反直觉。

**变更**：`proactive_sessions`（主动发送会话）语义改为与 `whitelist_sessions`（指令白名单）一致：

- **留空 = 所有会话都放行**（原版：留空 = 一个都不开，必须显式 opt-in）；
- 填入后仅列表内会话生效，`/表情包 主动 开启|关闭` 指令保留，用于免手填 UMO 地管理显式名单；
- 防打扰不靠这个列表兜底，仍由冷却 / 每日上限 / 安静时段 / 会话去重约束（三类主动共享同一份闸门状态）。

**连带修正**（原语义下诚实、新语义下误导的提示）：

- `/表情包 主动 关闭`：列表为空时不再回「本会话本就不在列表中」，改为说明「列表为空 = 全部放行，无法只关单个会话」并给出建立显式名单的路径；
- `/表情包 主动`（查状态）：列表为空时显示「已开启（列表为空 = 全部会话放行）」，不再显示「未开启」；
- 同步修正文档字符串、帮助文本、`_conf_schema.json` 的 `proactive_sessions` hint、README 与 `docs/设计说明.md` 的语义描述。

**验证**：`uv run --no-project --python 3.12 python -m compileall -q main.py sticker_gate.py sticker_cache.py wuwa_source.py` 通过；`tests/test_static.py` / `test_gate.py` / `test_cache.py` / `test_wuwa_match.py`（后者需 `--with aiohttp`）全过；zip 复核套一层结构。

版本 v0.2.1 -> v0.2.2。

## v0.2.1 (2026-10-01)

**实测反馈修复**（来自 astrbot_plugin_mine_chat 联动运行日志）：

1. **图片下载 401**：票券链下载偶发 `401`（「本地缓存下载失败（回退直发链接）: 图片下载返回 401」）。
   根因方向：凭据原先挂在会话级 headers，图片链对 Bearer 的态度未知。改为
   **按请求携带凭据** + 下载做两段重试（配置了 Token 时先无凭据、401 再带凭据），
   同时覆盖「图片链不认 Bearer」与「图片链需要鉴权」两种站方行为。
2. **跨插件 API 升级**：新增 `api_random_sticker(character="") -> dict | None`，
   返回 `{path, url, character, sticker_id}`——本地缓存失败时仍有票券链 URL 可供
   调用方兜底发送（旧 `api_random_sticker_path` 保留为兼容包装）。

## v0.2.0 (2026-10-01)

**新增跨插件 API**：`api_random_sticker_path(character="") -> str | None`

- 其他插件可通过 `context.get_registered_star("astrbot_plugin_moe_meme").star_cls.api_random_sticker_path()` 取一张随机（或指定角色的）表情，返回**本地缓存文件路径**，直接可用 `Image.fromFileSystem` 发送；
- token（api_token）、本地缓存目录、最近发过去重都由本插件管理，调用方零配置——首个使用方是 `astrbot_plugin_mine_chat`（萌萌日程）的主动表情通道，替代了此前「直接 import 本插件 wuwa_source 模块自建连接」的做法；
- API **不做**冷却/每日上限/安静时段（那是本插件主动通道自己的策略），调用方自行节流；
- 顺带把 `@register` 装饰器里滞留的 `v0.1.0` 版本号修正，与 metadata.yaml 保持同步。

## v0.1.3 (2026-09-30)

**现象**：回复概率跟图（`reply_sticker`）基本不生效，概率设成 1 也一样；顺带发现关键词概率触发（默认关）也从未生效过。

**根因：两个钩子在调用协程方法时漏了 `await`。**

```python
    async def on_llm_reply(self, event, response=None):
        try:
            self._reply_sticker_tick(event)   # ← 少了 await，协程对象被创建后直接丢掉
```
```python
    async def on_keyword(self, event):
        try:
            self._keyword_tick(event)         # ← 同上
```

Python 不会因此抛异常（只在 GC 时留一条 `RuntimeWarning: coroutine ... was never awaited`），AstrBot 也不会把它算作插件异常，所以这两个判定函数**一次都没执行过** —— 跟图与关键词触发从 v0.1.0 起就是完全死的：挂载钩子里永远没有 payload，什么都没发生。

**修复**

- 两处补 `await`。跟图与关键词触发现在真的会跑了。
- 流式输出下的交付方式改对：流式回复的最终结果是 `STREAMING_FINISH`，而 `ResultDecorateStage` 会被 `RespondStage` 直接丢弃（不会再发一次），把表情追加进那条链等于丢掉。改为在 `on_decorating_result` 里**单独补发**（文本已随流发完，顺序天然在回复之后）。
- 预判核心的**长文本转图**：命中时核心会把整条结果链替换成一张渲染图（后加的图会一起丢），此时不追加、留给 `after_message_sent` 补发（那时渲染图已发出）。
- 「没跟图」现在有迹可循：`enable` 开着却发不出来时，按会话+原因打一条 INFO（会话不在主动发送列表 / 冷却 / 每日上限 / 安静时段 / 数据源不可用），同原因只打一次，不刷屏。冷却那条会顺带提示「冷却与限额是三类主动共用一个计数，想每次都跟图可把冷却设为 0」。

**新增 `tests/test_static.py`**（纯 AST，不需要 astrbot 运行时）：守三类真出过的静默坑 ——

1. `self.<协程方法>(...)` 漏 `await`（本版那两个 bug）；
2. 事件钩子（`on_llm_response` / `on_decorating_result` / `after_message_sent` 等）写成了异步生成器 —— AstrBot 的 `call_event_hook` 里有 `assert inspect.iscoroutinefunction`，带 `yield` 会在运行期直接抛异常；
3. 指令回执又走回结果链（`event.plain_result / image_result / chain_result`）—— 会被核心按全局「回复时 @ 发送人」插 `At`（v0.1.2 改直发就是为了这个）。

**验证**：`compileall` 通过；`tests/test_static.py` / `test_gate.py` / `test_cache.py` / `test_wuwa_match.py` 全过；把 v0.1.2 的 `main.py` 喂给 `test_static.py` 的检查逻辑，能精确报出那两行漏 `await`（回放验证守卫有效）；zip 复核 9 条目套一层。

版本 v0.1.2 -> v0.1.3。

## v0.1.2 (2026-09-30)

**现象**：`/表情包` 出图时 bot 会先 @ 发指令的人；配置里把「指令发送时 @ 触发者」（`at_sender`）关掉后**依然 @**。

**根因**：这个 @ 不是插件加的，是 AstrBot 核心加的。`astrbot/core/pipeline/result_decorate/stage.py` 的 `ResultDecorateStage` 对「只含 `Plain` / `Image` 的结果链」会在最前面插一个 `At(发送者)`，开关是**全局**的 `platform_settings.reply_with_mention`（「回复时 @ 发送人」），且只在群聊生效、私聊不 @。而：

- 表情包回执恰好就是一张 `Image`（`[Image]` 满足 `can_decorate`），所以每条指令回图都自带一个 @；
- 插 At 发生在**所有插件 `on_decorating_result` 钩子跑完之后**，插件撤不掉它 —— `at_sender` 只能在「自己再加一个 At」和「什么都不做」之间选择，关了也不会让核心不加。

**修复（后端）**

- 指令回执改为**直发**：新增 `_send_text()` / `_send_chain()`（`await event.send(MessageChain(...))`，与 llm 工具 / 关键词触发 / 跟图兜底三条路径一致），`/表情包` 与 `白名单|主动` 管理子指令的所有回执（图 + 文字）不再走结果链，因此不再被核心装饰、也不会被插 At。是否 @ 触发者改由本插件的 `at_sender` 决定（开启时由插件自己插 `At`，带上昵称）。
- 顺带收益：`event.send()` 会置 `_has_send_oper`，本轮不会再触发一次默认 LLM 回复。
- **代价（刻意接受）**：结果链上的后续装饰对指令回执不再生效 —— 长文本转图、分段回复、TTS、回复前缀。对表情图无影响；`/表情包 列表` 这类长文本会原样发文字，不再被核心转成图片。做法与同工作区 `astrbot_plugin_model_panel` 的指令一致。

**同步**：`_conf_schema.json` 的 `at_sender` 提示、README（配置表 + 已知限制）、`docs/设计说明.md`（新增设计决定第 9 条）都写明「指令回执直发、不受全局 @ 设置影响」。

**验证**：`uv run --no-project --python 3.12 python -m compileall -q main.py sticker_gate.py sticker_cache.py wuwa_source.py` 通过；`tests/test_gate.py` / `tests/test_cache.py` / `tests/test_wuwa_match.py`（后者需 `--with aiohttp`）全过；`_conf_schema.json` JSON 校验通过；zip 复核套一层结构。

版本 v0.1.1 -> v0.1.2。

## v0.1.1 (2026-09-29)

**需求**：把每次取到的表情存起来；指令发送可选 @ 触发者（默认不 @）；bot 每次回复按概率决定是否跟一张表情。

**新增（后端）**

- 本地表情缓存：新增顶层模块 `sticker_cache.py`（纯 stdlib 可测）。每次取到表情先落盘到 `data/plugin_data/astrbot_plugin_moe_meme/cache/`（走 `StarTools.get_data_dir`，插件更新被 `remove_dir` 也不会清掉），指令 / llm 工具 / 关键词触发三条发送路径统一改为「优先本地文件，下载失败回退直发站方票券链」。票券链约 16 分钟失效，落盘后发送更稳更快。重启后内存索引丢失靠按 id 的 glob 找回；`cache_max_files`（默认 500，0 = 不淘汰）按最旧淘汰，防无限增长。仅作本机器人发送的运行时缓存，不用于二次配布。
- 指令可选 @：新增 `at_sender` 配置（默认 `false`），开启后 `/表情包` 出图前插入 `At` 组件 @ 触发者；管理子指令与文本回复不受影响。
- 回复概率跟图：新增 `reply_sticker` 配置块（`enable` 默认关 / `probability` 0.1 / `character` 固定角色可空 / 冷却 / 限额 / 安静时段）。实现走三个钩子：`on_llm_response` 判定（开关、主动列表、概率、闸门）并预取表情暂存到事件 extra → `on_decorating_result` 把图并入本轮回复链（**文字+图一条消息**）→ `after_message_sent` 兜底（AstrBot 流式输出下官方明确 `on_decorating_result` 可能不生效，此时回复后单独补发）。与 llm 工具、关键词触发**共享同一份会话冷却状态**，天然防止同一条回复里既走工具又跟图连发两张。

**验证**：`compileall` 全过；`tests/test_gate.py` / `tests/test_wuwa_match.py` / 新增 `tests/test_cache.py`（safe_id 清洗、store/find_existing 往返、prune 最旧淘汰、异常目录安全）全过；`_conf_schema.json` JSON 校验通过；zip 复核 9 条目套一层。

版本 v0.1.0 -> v0.1.1。

## v0.1.0 (2026-09-29)

首版发布。

- 指令：`/表情包` 全库随机；`/表情包 <角色>` 按角色（站方全名 / 英文 id / 别名 / 模糊包含，多个命中取张数最多者）；`/表情包 列表`；`/表情包 帮助`；`/表情包 白名单 开启|关闭`、`/表情包 主动 开启|关闭`（管理员，免手填 UMO）。
- 白名单语义：`whitelist_sessions` 非空仅列表内会话可用、留空全放行；白名单外静默忽略；管理子指令不受白名单限制（否则白名单外的管理员无法把自己加进来）。
- 主动发送：LLM 工具 `send_sticker`（对话中由模型自主决定氛围，每次回复最多一次）+ 关键词概率触发（默认关，词表可配置、词可映射角色）。仅对显式 `proactive_sessions` 会话生效——刻意不随白名单「空=全放行」，避免空配置在所有群乱发。
- 防打扰护栏：会话冷却（LLM 10min / 关键词 30min）、每日上限 20 张/会话、安静时段 01:00-08:00、会话内最近 20 张去重（指令与主动共用，撞上去重最多重试 2 次）；两类主动共享同一份会话状态。
- 数据源（emoji.wuwa.games 表情包仓鼠库，CC BY-NC-SA）：随机接口 `random` + 角色聚合接口 `archive-index`（24h TTL 缓存）；图片为站方短时效票券链（约 16 分钟有效），一律现拉现发、不缓存不落盘，符合站方「请勿把临时图片地址当作永久外链」「二次配布需联系站长」的要求。
- 热重载：`__init__` 按依赖序强制重载 `sticker_gate` / `wuwa_source`（AstrBot 热更新只重载 main.py 的坑）。
- 已知限制：站方 Token 携带方式官方未公布，`api_token` 按 `Authorization: Bearer` 预留，匿名可用。

版本 v0.1.0（首版）。
