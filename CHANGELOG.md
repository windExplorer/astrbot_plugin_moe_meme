# 更新日志

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
