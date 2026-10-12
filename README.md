# Scryfall 卡图下载器

使用 Python 标准库编写的桌面工具。输入中英文牌表，优先选择中文卡图，下载到共享图片库，并生成适合打印的卡牌 PDF。

## 功能

- Tkinter 图形界面和命令行下载引擎。
- 简体、繁体、英文语言优先顺序；中文文件名和双面牌处理。
- GUI 不显示并发选项；查询默认 4 线程，图片下载默认 8 线程。命令行 `--workers` 仅控制查询（1–16），`--download-workers` 控制下载（1–32）。
- API 请求间隔仍为 0.6 秒；图片默认无固定启动间隔，可用 `--image-interval` 调整；遵守 Retry-After，并在受限时降并发和停止持续失败的请求。
- 自动检查主卡与所选打印的关联 token / 徽记，按身份去重下载，默认每种身份在主卡之后追加一份到 PDF。
- 所有任务共用 `卡图输出/pics`；已缓存的中英文牌名直接复用图片，跳过查询和下载。
- 图片缺失或损坏时使用保存的 URL 补下载；PNG 分块 CRC 检查。
- 自动迁移旧任务的图片，按内容去重，保留同名不同版本。
- 任务 PDF、牌表、清单和日志按 `yymmdd-hhmmss` 分目录保存。
- A4 竖向每页 9 张，A3 横向每页 18 张；卡牌尺寸 63×88 mm，可选择裁切框。

## 运行

需要 Python 3.10 或更新版本，以及 Tkinter/Tcl/Tk。不需要安装第三方 Python 包。网络访问需要能够连接 Scryfall。

Windows 用户可以双击 `启动GUI.cmd`，也可以运行：

```powershell
python scryfall_cards_gui.pyw
```

每行使用一种牌名，可带数量：

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
python scryfall_cards_threaded.py deck.txt --out results --pics ./卡图输出/pics --workers 4
python scryfall_cards_threaded.py deck.txt --image-interval 0.1
```

已有图片优先于新的语言偏好；需要换版本时，在 GUI 勾选“重新选图并下载（忽略图片缓存）”，或使用 `--force`。

## 关联 token 与缓存兼容

程序合并解析对象和实际所选打印的 `all_parts`，只收 `component=token`，通过具体卡牌 ID 查询，不按通用名称猜图，也不递归反向关联。可靠 oracle ID 用于寻找同一身份的语言打印，缺失时保持具体打印；实际语种写入清单。同名不同规格保留独立身份与文件。双面 token 下载所有有效面，PDF 按面顺序追加一份，不根据主卡数量推算生成数量。

JSON 保留主卡 `cards` / `summary`，新增 `tokens` / `token_summary`；主卡记录 `token_check` 与 `token_refs`。查询或下载 token 失败时仍输出主卡与已成功的 token，返回码为 1，GUI 显示部分失败。规则可能创建 token 却没有关联对象时明确提示，不生成推测的名称或图片。

旧索引继续复用主图：有保存的打印 ID，或可靠 Scryfall 图片 URL 中的 ID 时首次补查一次；成功的无关联结果也缓存。无可靠 ID 的自制图标记无法自动检查，不猜关系。失败检查下次重试；完整 token 缓存无额外 API/下载，缺失或损坏的图片按已知 URL 补下载。`--force` 同步刷新主卡版本和关系。`--dry-run` 记录选图及关系，不下载也不生成 PDF。

`--image-interval` 接受非负有限秒数，0 表示取消固定图片启动间隔，但线程上限、共享冷却及 Retry-After 仍生效；负数、NaN、无穷大被拒绝。它不改变 API 的独立限速。首次图片请求与普通网络错误重试使用同一个时钟。缩短间隔不代表服务端长期吞吐已验证，GUI 默认不变。

## 文件与开发

- `scryfall_cards_threaded.py`：下载、图片缓存和 PDF 引擎。
- `gui_layer.py`：GUI 源码。
- `build_gui.py`：将引擎与 GUI 合并为可独立运行的 `scryfall_cards_gui.pyw`。
- `GUI使用说明.txt`：完整选项与输出说明。

修改源代码后重建 GUI：

```powershell
python build_gui.py
```

运行离线及本地 HTTP 测试（GUI 测试需要可用的桌面/Tk 环境）：

```powershell
python -B -m unittest test_tokens test_shared_picture_cache test_scryfall_threaded test_scryfall_gui test_parallel_queries test_network_policy -v
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
