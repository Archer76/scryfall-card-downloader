# Scryfall 卡图下载器

使用 Python 标准库编写的桌面工具。输入中英文牌表，优先选择中文卡图，下载到共享图片库，并生成适合打印的卡牌 PDF。

## 功能

- Tkinter 图形界面和命令行下载引擎。
- 简体、繁体、英文语言优先顺序；中文文件名和双面牌处理。
- 查询与下载共用线程数：默认 4，支持 1–16。
- 全局 API 请求间隔 0.6 秒、图片请求启动间隔 0.2 秒；遵守 Retry-After，并在受限时降并发和停止持续失败的请求。
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
```

已有图片优先于新的语言偏好；需要换版本时，在 GUI 勾选“重新选图并下载（忽略图片缓存）”，或使用 `--force`。

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
python -B -m unittest test_shared_picture_cache test_scryfall_threaded test_scryfall_gui test_parallel_queries test_network_policy -v
```

Windows 启动器测试：

```powershell
python -B test_gui_launcher.py
```

这些测试不请求真实 Scryfall 服务。程序运行产生的卡图、牌表、缓存、PDF 和日志不属于源码，已加入忽略规则。

## 网络行为

线程数是客户端并发设置，图片启动间隔也是客户端策略；它们不代表服务端允许的长期并发上限。连接不稳定时可以将线程数降到 2。

Scryfall API 的具体限额可能变化，请参考 [官方速率限制说明](https://scryfall.com/docs/api/rate-limits)。

## 许可证

程序源码采用 MIT 许可证，见 [LICENSE](LICENSE)。卡图来自 Scryfall；MIT 许可证仅适用于本仓库的程序代码，卡牌图像和相关内容仍归各自权利人所有。仓库不包含卡图或用户运行数据。
