---
name: md2epub
description: 把结构化 Markdown 转成合规、好看、不掉图的 EPUB 3，按条/篇自动分章、两级目录、脚注双向链接、宽表转卡片、生成封面，并做 EPUB 3.3 结构校验与掉图检查。当用户说"转epub"、"做成电子书"、"放进微信读书"、"Kindle 上看"、"md 转电子书"、"epub 排版"时使用。也用于验收别人给的 md 是否具备转书条件（lint 子命令）。不适用：docx/pdf 互转（用 convert-to-md / md2word / pdf 技能）。
version: 1.0.0
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
---

# md2epub — Markdown 转 EPUB

四条验收线，缺一不可：**能转（结构对）、好看（手机可读）、合规（EPUB 3.3）、不掉图（资源全在）**。

## 先决条件：MD 输入契约

本技能假定输入 md 满足 `references/md-contract.md` 的约定，最关键六条：

1. 标题层级连续、不跳级；书名用一级标题，部分用二级，篇/条用三级，小节用四级。
2. 一行一段，段内不硬换行；段落之间空行分隔。
3. 表格是标准管道表，表头唯一、列数一致，无 `<fcel>/<nl>` 之类残留。
4. 脚注写成 `> ① 注释正文`，紧跟在引用它的章节内；正文角标用 ①②③。
5. 图片用 `![替代文字](相对路径)`，路径只含 ASCII、大小写与磁盘一致。
6. 不内嵌目录页、页眉页脚、页码（有也会被 `build` 剥离，但最好在 md 侧清掉）。

输入不达标时**不要硬转**，先跑 `lint` 把问题打回 md 生产端。

## 工作流（五步）

```bash
S=~/.agents/skills/md2epub/scripts/md2epub.py

# 1. 检查输入
python3 $S lint book.md

# 2. 规划：看标题分布、表格列数、超长章，生成配置
python3 $S plan book.md --write-config book.json

# 3. （扫描件）从原 PDF 抽封面；比程序生成的更像一本书
python3 $S cover book.pdf --out cover.jpg --page 0 --crop 0.04,0,0,0

# 4. 填配置后构建（构建结束自动跑 check）
python3 $S build book.md --config book.json --out book.epub --preview /tmp/preview

# 5. 单独复检（改包、换工具链之后都要跑）
python3 $S check book.epub
```

`lint` 有阻断项时退出码为 1；`build` 内置 `check`，不通过也会返回非零。

## 封面优先级

1. **原书封面**：`cover` 子命令从 PDF 指定页**渲染**取图（不是抽内嵌图，避免反色和掩膜问题），可裁边、缩放、压成 JPEG。
2. **自备图片**：`build --cover x.jpg` 或配置 `cover.image`。
3. **程序生成**：都没有时用书名排版生成（Pillow，缺库则退化为纯文字封面页）。

扫描封面常见问题：装订边留下白色/灰色竖条（`cover` 会打印四条边与内部的亮度差，按提示用 `--crop 左,上,右,下` 按百分比裁）、页面有反色（必须渲染而不是抽图）、尺寸过大（`--max-side` 默认 1600，压到 200KB 左右）。

**封面太暗会被平台"吃掉"**：`cover` 与 `check` 都会报平均亮度，低于 40/255 时提示「书架缩略图可能看不出内容」（肖峰那本实测 16/255，微信读书不显示）。遇到这种情况换浅色版本或加浅色边框，别只怀疑文件格式。

## 配置项

见 `templates/config.example.json`。要点：

| 键 | 作用 | 默认 |
|---|---|---|
| `title/subtitle/author/publisher/language/date` | 元数据，同时用于封面与书名页 | — |
| `theme` | `literary`（文学，居中标题）/ `legal`（法律，红色竖线）/ `technical`（技术，无缩进） | literary |
| `split_level` | 到第几级标题分章（2=按部分，3=按篇/条） | plan 建议值 |
| `max_chars` | 单章字数上限，超出按段落切「（续N）」 | 26000 |
| `table_card_min_cols` | 几列起改卡片式（手机可读） | 7 |
| `table_rows_per_chunk` | 长表每段行数（段段重复表头） | 30 |
| `footnotes` | `endnote` 章末注释 / `off` 不带注释 | endnote |
| `byline` | 标题下的短人名是否按作者行居中 | true |
| `cover.image` / `cover.accent` / `cover.title_lines` | 指定封面图 / 生成封面底色 / 手动断行 | 深蓝 |

## 输出保证（skill 的承诺）

- **分章**：每个 `split_level` 及更高级标题独立成 XHTML，超长章自动续编，避免整本一个文件。
- **目录**：`nav.xhtml`（EPUB3）+ `toc.ncx`（兼容旧引擎）双份，两级嵌套。
- **脚注**：章内统一编号、去重、正文角标 ↔ 章末注释双向链接。
- **宽表**：≥ `table_card_min_cols` 列改为「字段名＋值」卡片；长表分段并重复表头。
- **图片**：复制进 `OEBPS/images/`，文件名转 ASCII 小写，逐张写入 manifest 并校验引用；`alt` 为「第 N 页图」这类占位文字时不显示成图注（只留作无障碍文字）。
- **合规**：mimetype 首项且不压缩、OPF 元数据齐全、manifest/spine 自洽、XHTML 合法、内部链接与图片路径逐条核对（区分大小写）。
- **体检**：`check` 另报四类经验性风险——封面过暗（`W_COVER_DARK`）、图片过小（`W_IMG_SMALL`）、单图超 2 MB（`W_IMG_LARGE`）、整包超 50 MB（`W_BIG_BOOK`）。
- **元数据兜底**：配置里没写 `title`/`subtitle` 时，自动取 md 的一级标题与首条引用。

## 已知边界

- 不生成音频、不生成固定版式（fixed layout）、不做 DRM。
- 不处理远程图片；`http(s)://` 图片会保留原样并提示，需先下载到本地。
- 上游是 PDF 时：**有文字层就用文字层，没有才用 Xiaomi-OCR（本机 oMLX）**。文字层常带行尾空格，清掉即可；小米在图表页会丢正文，不要无条件用它覆盖文字层。
- 复杂合并单元格的表格会被展开成多列，转换前应在 md 侧整理。
- `check` 覆盖 EPUB 3.3 的结构硬性项，不等同于官方 epubcheck 的全部语义校验；需要严格合规时另装 epubcheck（Java）再跑一遍。

## 常见坑（改脚本前先看）

1. **掉图九成是大小写或路径**：包内路径区分大小写，macOS 不区分，本机正常、手机掉图。
2. **mimetype 必须是 zip 第一项且 `ZIP_STORED`**，用第三方工具重打包很容易破坏。
3. **不要用 SVG 包位图**，部分阅读器不渲染；封面页用 `<img>`。
4. **微信读书会转码**：CSS 大范围覆盖、SVG 可能丢，所以样式只放在样式表里，别写在 SVG 里。
5. **脚注编号按章重排**，不要沿用原书按页编号，否则一章里会出现十几个 ①。
