# md2epub

把结构化 Markdown 转成**合规、好看、不掉图**的 EPUB 3 —— 给微信读书、Apple Books、Kindle 看的电子书。

四条验收线：能转（结构对）、好看（手机可读）、合规（EPUB 3.3）、不掉图（资源全在）。

## 能做什么

- **分章**：按标题层级切成独立 XHTML，超长章自动续编，不会整本一个文件
- **目录**：`nav.xhtml`（EPUB3）+ `toc.ncx`（兼容旧引擎）双份，支持任意深度（编 → 类别 → 法规 → 节）
- **脚注**：章内统一编号、去重，正文角标 ↔ 章末注释双向链接
- **宽表**：7 列以上自动改成「字段名＋值」卡片，手机上不再横向溢出；长表分段并重复表头
- **图片**：入库、转 ASCII 名、逐张写 manifest，路径大小写校验，`alt` 占位文字不显示成图注
- **封面**：优先从原书 PDF 渲染取图，可自动裁边、提示亮度（太暗会被平台"吃掉"）
- **体检**：mimetype、OPF 元数据、manifest/spine、XHTML、内部链接、图片尺寸与体积

## 快速开始

```bash
S=./scripts/md2epub.py

# 1. 检查 Markdown 是否符合输入契约
python3 $S lint book.md

# 2. 统计结构，生成配置建议
python3 $S plan book.md --write-config book.json

# 3. （可选）从原书 PDF 取封面
python3 $S cover book.pdf --out cover.jpg --page 0 --trim

# 4. 生成 EPUB（结束自动跑校验）
python3 $S build book.md --config book.json --out book.epub --preview /tmp/preview

# 5. 单独复检
python3 $S check book.epub
```

只依赖 Python 3 标准库；封面生成与图片体检需要 Pillow（缺失时自动降级）。
从 PDF 取封面需要 PyMuPDF（`pip install pymupdf`）。

## 输入契约

md 必须满足 `references/md-contract.md` 的约定，关键六条：

1. 标题层级连续、不跳级；书名一级、部分二级、篇/条三级、小节四级
2. 一行一段，段内不硬换行
3. 标准管道表，表头唯一、列数一致，无 `<fcel>` 之类残留
4. 脚注写成 `> ① 注释正文`，正文角标用 ①②③
5. 图片 `![替代文字](相对路径)`，路径只用 ASCII
6. 不内嵌目录、页眉页脚、页码

`lint` 会把不合格的地方逐条列出来（阻断项 / 提醒项）。

## 合规与校验

`check` 覆盖 EPUB 3.3 的结构硬性项：mimetype 首项且不压缩、OPF 唯一标识与 `dcterms:modified`、manifest 覆盖全部资源、spine 自洽、XHTML 格式良好、内部链接与图片路径（区分大小写）可解析、图片有 alt。

详见 `references/epub-checklist.md`。要过官方 epubcheck（需 Java）时另跑：

```bash
java -jar epubcheck.jar book.epub
```

## 主题

| theme | 适用 |
|---|---|
| `literary` | 文学、学术专著，章题居中 |
| `legal` | 法律文本，章题带红色竖线 |
| `technical` | 技术文档，无首行缩进 |

## 已知边界

- 不生成音频、固定版式（fixed layout）、不做 DRM
- 不处理远程图片，需先下载到本地
- 图片型表格只能整图呈现，不可检索
- 上游是 PDF 时：**有文字层就用文字层，没有才用 OCR**；OCR 在图表页会丢正文，不要无条件用它覆盖文字层

## 许可

内部工具，未附开源许可证。
