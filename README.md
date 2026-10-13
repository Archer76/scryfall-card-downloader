# Scryfall 卡图下载器

使用 Python 标准库编写的桌面工具。输入中英文牌表，优先选择中文卡图，下载到共享图片库，并生成适合打印的卡牌 PDF。

## 功能

- Tkinter 图形界面和命令行下载引擎。
- 简体、繁体、英文语言优先顺序；中文文件名和双面牌处理。
- 游戏选择：万智牌、宝可梦TCG（中文）、宝可梦TCG（英文）、游戏王（简中）、游戏王（日文）、游戏王（TCG）。后三项仅占位，不启动下载。
- 宝可梦中文仅访问 TCGdex `zh-tw`，英文仅访问 `en`；按指定系列和编号选图，不跨语言替换或同名重印替换。查询／下载默认 64／64，万智牌保持 4／8；任务运行期间禁止切换游戏。
- PTCG 的 64／64 是本机实测择优预设，不是官方限额。2026-10-13 对相同样本比较 16／32／64：名称搜索与详情每档各 256 请求、30 秒 socket 超时，64 的综合耗时最低；同样 128 张高清 PNG（48.150 MB）、120 秒 socket 超时，三档分别耗时 83.469／81.328／68.703 秒，全部成功。若低档达到最高吞吐的 95% 会优先低档，本次两项均选择 64。详见[测试报告](tcgdex-benchmark-results/report.md)。
- [TCGdex FAQ](https://tcgdex.dev/faq#are-there-rate-limits) 未公布硬性请求限额，并建议缓存查询结果。PTCG 查询使用独立 API 缓存与冷却，不改变 Scryfall 的 0.6 秒 API 间隔；图片下载共用成熟的完整性校验、Retry-After、降并发保护。
- 命令行 `--game mtg/ptcg-zh/ptcg-en` 选择数据源；万智牌 `--workers` 默认 4（1–16），`--download-workers` 默认 8（1–32）；宝可梦两参数默认 64（1–64）。
- API 请求间隔仍为 0.6 秒；图片默认无固定启动间隔，可用 `--image-interval` 调整；遵守 Retry-After，并在受限时降并发和停止持续失败的请求。
- 自动检查主卡与所选打印的关联 token / 徽记，按身份去重下载，默认每种身份在主卡之后追加一份到 PDF。
- 图片目录：万智牌 `卡图输出/mtg-pics`；宝可梦中文 `卡图输出/ptcg-pics/zh-tw`；宝可梦英文 `卡图输出/ptcg-pics/en`。宝可梦索引按语言、系列及编号存储，不以同名合并。
- 图片缺失或损坏时使用保存的 URL 补下载；PNG 分块 CRC 检查。
- 自动迁移真正旧任务的 `cards` 图片，按内容去重，保留同名不同版本；旧 `pics` 已弃用，不自动读取、迁移或回流其清单 URL，也不导入另一游戏图片目录。
- 任务 PDF、牌表、清单和日志按 `yymmdd-hhmmss` 分目录保存。
- A4 竖向每页 9 张，A3 横向每页 18 张；卡牌尺寸 63×88 mm，可选择裁切框。

## 运行

需要 Python 3.10 或更新版本，以及 Tkinter/Tcl/Tk。不需要安装第三方 Python 包。网络访问需要能够连接 Scryfall。

Windows 用户可以双击 `启动GUI.cmd`，也可以运行：

```powershell
python scryfall_cards_gui.pyw
```

每行使用一种牌名，必须带数量：

```text
4 Lightning Bolt
4 Counterspell
8 Forest
1 碎骨巨人
```

中英文名称不要在同一行用斜杠并列。

命令行用法：

```powershell
python scryfall_cards_threaded.py deck.txt --out results --paper A4
python scryfall_cards_threaded.py deck.txt --out results --no-pdf
python scryfall_cards_threaded.py deck.txt --out results --pics ./卡图输出/mtg-pics --workers 4
python scryfall_cards_threaded.py deck.txt --image-interval 0.1
```

## 宝可梦牌表

两种宝可梦语言分别使用本语言发行的系列与卡号，不相互转换。每个有效行必须是 `数量 卡名 系列代码 编号`；任意数字开头行缺少版本信息，整个任务拒绝运行并提示格式。万智牌与宝可梦都跳过首个非空字符不是数字的行，支持直接粘贴分类标题与总数；万智牌也必须写数量。游戏王占位不使用这条过滤规则。

```text
Pokémon:4
2 Shaymin DRI 10
1 Froslass PR-SV 117
1 Tatsugiri PR-SV 118
Total Cards:4
```

```powershell
python scryfall_cards_threaded.py deck.txt --game ptcg-en --out results
python scryfall_cards_threaded.py 中文牌表.txt --game ptcg-zh --out results
```

英文支持官方短代码（例如 DRI、TWM、ASC）以及 TCGdex set ID；`PR-SV` 是 `svp` 的显式导出兼容别名。中文使用本语言的 set ID，例如 `SV2a`。卡号从系列实际列表唯一匹配后获取真实 ID（10 可匹配 010），不猜补零。详情的卡名、系列和编号均需一致；同名不同招式/版本不互换。`Basic Darkness Energy` 等 SVE 能量导出名只在准确系列和卡号已锁定时兼容。

缺集、缺卡、错名、身份冲突、缺图和请求失败分别写入清单；失败不缓存成成功，已有成果保留。已证实 `SVE 23` 有资料但缺图片，程序报告缺图，不猜 URL。PDF 按牌表顺序与数量排版，PTCG 不进行万智牌关联 token 查询。完整缓存再次运行零 API/图片请求，损坏或缺失图片按已保存 URL 补下载。仅查询不下载/PDF，强制刷新重新查询所指定版本。

CLI 默认目录随游戏决定；`--pics` 可显式指定缓存目录，程序不会自动删除该目录。旧 `pics` 数据不自动清理，游戏切换同步目录标签和打开目标。

已有图片优先于新的语言偏好；需要换版本时，在 GUI 勾选“重新选图并下载（忽略图片缓存）”，或使用 `--force`。

## 关联 token 与缓存兼容

程序合并解析对象和实际所选打印的 `all_parts`，只收 `component=token`，通过具体卡牌 ID 查询，不按通用名称猜图，也不递归反向关联。可靠 oracle ID 用于寻找同一身份的语言打印，缺失时保持具体打印；实际语种写入清单。同名不同规格保留独立身份与文件。双面 token 下载所有有效面，PDF 按面顺序追加一份，不根据主卡数量推算生成数量。

JSON 保留主卡 `cards` / `summary`，新增 `tokens` / `token_summary`；主卡记录 `token_check` 与 `token_refs`。查询或下载 token 失败时仍输出主卡与已成功的 token，返回码为 1，GUI 显示部分失败。规则可能创建 token 却没有关联对象时明确提示，不生成推测的名称或图片。

当前万智牌图片目录内的旧格式索引继续复用主图（不包括已弃用的 `pics` 索引）：有保存的打印 ID，或可靠 Scryfall 图片 URL 中的 ID 时首次补查一次；成功的无关联结果也缓存。无可靠 ID 的自制图标记无法自动检查，不猜关系。失败检查下次重试；完整 token 缓存无额外 API/下载，缺失或损坏的图片按已知 URL 补下载。`--force` 同步刷新主卡版本和关系。`--dry-run` 记录选图及关系，不下载也不生成 PDF。

`--image-interval` 接受非负有限秒数，0 表示取消固定图片启动间隔，但线程上限、共享冷却及 Retry-After 仍生效；负数、NaN、无穷大被拒绝。它不改变 API 的独立限速。首次图片请求与普通网络错误重试使用同一个时钟。缩短间隔不代表服务端长期吞吐已验证，GUI 默认不变。

## 文件与开发

- `scryfall_cards_threaded.py`：万智牌引擎、通用下载/PDF与游戏 CLI 路由。
- `ptcg_engine.py`：TCGdex 精确系列编号查询、独立语言缓存与任务输出。
- `gui_layer.py`：GUI 源码。
- `build_gui.py`：将引擎与 GUI 合并为可独立运行的 `scryfall_cards_gui.pyw`。
- `GUI使用说明.txt`：完整选项与输出说明。

修改源代码后重建 GUI：

```powershell
python build_gui.py
```

运行离线及本地 HTTP 测试（GUI 测试需要可用的桌面/Tk 环境）：

```powershell
python -B -m unittest discover -v
```

Windows 启动器测试：

```powershell
python -B test_gui_launcher.py
```

这些测试不请求真实 Scryfall 服务。程序运行产生的卡图、牌表、缓存、PDF 和日志不属于源码，已加入忽略规则。

## 网络行为

2026-10-12 经本机代理测试图片下载，固定启动间隔为 0；4/8/16/32 并发各取 48 张不同 PNG，全部成功，无 403/429/503。耗时分别为 48.77/41.32/56.38/48.81 秒，8 并发吞吐最高（1.162 张/秒），因此默认采用 8 并发、零固定间隔。8 和 32 档各有一次 SSL EOF，内部重试后成功。该小样结果仅适用于当时链路，不证明服务端真实上限，也不保证长期无阻塞；受限保护仍保留。查询并发与 API 间隔未改变。

线程数是客户端并发设置，图片启动间隔也是客户端策略；它们不代表服务端允许的长期并发上限。连接不稳定时可以用 `--download-workers 2 --image-interval 0.2` 降低下载强度。

Scryfall API 的具体限额可能变化，请参考 [官方速率限制说明](https://scryfall.com/docs/api/rate-limits)。

## 许可证

程序源码采用 MIT 许可证，见 [LICENSE](LICENSE)。卡图来自 Scryfall；MIT 许可证仅适用于本仓库的程序代码，卡牌图像和相关内容仍归各自权利人所有。仓库不包含卡图或用户运行数据。
