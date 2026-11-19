# EPUB 合规与图片检查清单

`md2epub.py check` 覆盖下表的“结构”部分。要更严格的语义校验（CSS 属性合法性、EPUB 专有属性用法），另装官方 epubcheck（需 Java）：

```bash
brew install openjdk
curl -L -o /tmp/epubcheck.zip https://github.com/w3c/epubcheck/releases/latest/download/epubcheck-5.1.0.zip
unzip -q /tmp/epubcheck.zip -d /tmp
java -jar /tmp/epubcheck-5.1.0/epubcheck.jar book.epub
```

## 一、标准基线

| 项 | 要求 | 出处 |
|---|---|---|
| 版本 | EPUB 3.3（W3C Recommendation, 2023-05-25） | EPUB 3.3 |
| 兼容 | 同时提供 `toc.ncx`，兼容 EPUB2 引擎 | EPUB 2.0.1 遗留 |
| 无障碍 | 图片有 alt、标题层级语义正确、声明语言 | EPUB Accessibility 1.1（WCAG 2.0 AA 起） |
| 包结构 | OCF：`mimetype` + `META-INF/container.xml` + OPF | OCF 3.3 |

## 二、结构硬性项（check 逐条验）

1. `mimetype` 是 zip **第一项**，内容恰为 `application/epub+zip`，`ZIP_STORED` 不压缩。
2. `META-INF/container.xml` 存在，`full-path` 指向真实存在的 OPF。
3. OPF 有 `unique-identifier`，且该 id 对应一个 `dc:identifier`。
4. OPF 有 `dcterms:modified`（EPUB3 必需），格式 `YYYY-MM-DDThh:mm:ssZ`。
5. `dc:title`、`dc:language` 存在。
6. manifest 覆盖包内所有资源；每个 `href` 指向的文件真实存在。
7. spine 至少一项，`idref` 都能在 manifest 找到。
8. 有 `properties="nav"` 的导航文档；导航能到达各顶层章节。
9. 每个 XHTML 是格式良好的 XML。
10. 所有 `href`/`src` 内部引用可解析；**大小写与磁盘一致**。
11. 不引用远程资源（`http(s)://` 出现在 manifest 视为不合规）。
12. 同一文档内 id 不重复。

## 三、图片专项

| 检查 | 说明 |
|---|---|
| 文件存在 | `src` 解析后的路径必须在包内，且大小写完全一致 |
| manifest 声明 | 每张图都有 `<item>`，`media-type` 与扩展名匹配 |
| 核心格式 | PNG / JPEG / GIF / SVG；WebP、TIFF、BMP、HEIC 不用 |
| alt | 每张 `<img>` 都有 `alt`（无障碍 + 掉图兜底） |
| 尺寸 | 长边 ≤ 1600–2000 px，单张 ≤ 1.5–2 MB，整本 ≤ 50 MB |
| 封面 | manifest `properties="cover-image"` + spine 首页 `<img>`，两处齐 |
| SVG | 只用矢量，不拿 `<image xlink:href>` 包位图 |
| CSS | 不用 `background-image` 承载内容图（夜间模式会消失） |

## 四、平台差异

| 平台 | 接受格式 | 注意 |
|---|---|---|
| 微信读书 | EPUB/PDF/TXT/DOCX | 上传后二次转码：CSS 大量被覆盖、SVG 可能丢、脚注弹注不稳定；宽表务必卡片化 |
| Apple Books | EPUB 3 | 支持 SVG、固定版式；对 CSS 支持较好 |
| Kindle | EPUB（2022 起，后台转 KFX） | 封面单独声明；复杂 CSS 会被简化 |
| 多看／掌阅 | EPUB 2/3 | 对 `toc.ncx` 依赖较强，双目录更稳 |

## 五、掉图排查顺序

1. 解包，`ls OEBPS/images` 看文件是否在。
2. 对比 `src` 与磁盘文件名的大小写。
3. 看 OPF 里有没有这张图的 `<item>`，`media-type` 对不对。
4. 看是不是 `background-image` 或 SVG `<image>` 承载。
5. 换一个阅读器验证（排除单平台问题）。
