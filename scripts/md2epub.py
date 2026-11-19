#!/usr/bin/env python3
"""md2epub — 把结构化 Markdown 转成合规、好看、不掉图的 EPUB 3。

子命令：
    lint  <md>                 按 MD 输入契约检查（供 md 生产端自检）
    plan  <md> [--write-config cfg.json]
                               统计结构，给出分章建议并生成配置
    build <md> --config cfg.json [--out x.epub] [--preview DIR]
                               生成 EPUB
    check <epub>               合规 + 掉图 + 链接自检（EPUB 3.3 硬性项）

只依赖 Python 标准库；封面生成可选 Pillow，缺失时降级为纯文字封面页。
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
MEDIA_TYPES = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.svg': 'image/svg+xml',
}
BLOCKING = {'E_NO_HEADING', 'E_NO_CONTENT', 'E_IMAGE_MISSING', 'E_TABLE_TOKEN',
            'E_ENCODING', 'E_BAD_TABLE'}


# ---------------------------------------------------------------- 通用

def esc(text):
    return html.escape(text, quote=False)


def read_text(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f'文件不存在：{p}')
    return p.read_text(encoding='utf-8')


def slug_image(name, used):
    stem = re.sub(r'\.[^.]+$', '', name)
    ext = Path(name).suffix.lower()
    safe = re.sub(r'[^a-z0-9_-]+', '-', stem.lower()).strip('-') or 'img'
    cand = f'{safe}{ext}'
    i = 2
    while cand in used:
        cand = f'{safe}-{i}{ext}'
        i += 1
    used.add(cand)
    return cand


# ---------------------------------------------------------------- MD 解析

def strip_generated_toc(lines):
    """剥离 md 里自带的目录块（`## 目录` 到 `---`），避免与 nav 重复。"""
    for idx, line in enumerate(lines):
        if line.strip() in ('## 目录', '# 目录', '## 目 录'):
            end = idx
            while end < len(lines) and lines[end].strip() != '---':
                end += 1
            return lines[:idx] + lines[end + 1:], True
    return lines, False


def split_table(rows, rows_per_chunk=30):
    header = rows[0]
    body = [r for r in rows[1:] if not re.fullmatch(r'\|[\s:\-|]+\|', r.strip())]
    sep = '|' + '|'.join(['---'] * (header.strip().strip('|').count('|') + 1)) + '|'
    chunks = [body[i:i + rows_per_chunk] for i in range(0, len(body), rows_per_chunk)] or [[]]
    return [{'t': 'table', 'rows': [header, sep] + chunk} for chunk in chunks]


IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')


def parse_blocks(lines, rows_per_chunk=30):
    blocks, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            blocks.append({'t': 'h', 'level': len(m.group(1)), 'text': m.group(2).strip()})
            i += 1
            continue
        if line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                rows.append(lines[i])
                i += 1
            blocks.extend(split_table(rows, rows_per_chunk))
            continue
        if line.startswith('>'):
            blocks.append({'t': 'note', 'text': line.lstrip('>').strip()})
            i += 1
            continue
        blocks.append({'t': 'p', 'text': line.strip()})
        i += 1
    return blocks


def drop_title_block(blocks):
    """去掉 md 顶部的一级书名标题与紧随的引用式副标题（另有封面页）。"""
    if blocks and blocks[0]['t'] == 'h' and blocks[0]['level'] == 1:
        blocks = blocks[1:]
        if blocks and blocks[0]['t'] == 'note':
            blocks = blocks[1:]
    return blocks


# ---------------------------------------------------------------- lint

def lint_md(md_path):
    p = Path(md_path).expanduser().resolve()
    raw = p.read_bytes()
    findings = []

    def add(sev, code, line, msg):
        findings.append({'severity': sev, 'code': code, 'line': line, 'message': msg})

    if raw.startswith(b'\xef\xbb\xbf'):
        add('warn', 'W_BOM', 0, '文件带 UTF-8 BOM，部分阅读器会把书名首字读成乱码')
    text = raw.decode('utf-8', errors='replace')
    if '\ufffd' in text:
        add('error', 'E_ENCODING', 0, '存在无法解码的字符（U+FFFD），检查源文件编码')

    lines = text.split('\n')
    blocks = parse_blocks(lines)
    heads = [(i, b) for i, b in enumerate(blocks) if b['t'] == 'h']
    if not heads:
        add('error', 'E_NO_HEADING', 0, '没有任何标题，无法分章')
    if len(heads) < 2:
        add('warn', 'W_FLAT', 0, '只有一层标题，EPUB 目录无法分级')

    # 标题层级连续性
    levels = [b['level'] for _, b in heads]
    prev = None
    for idx, (_, b) in enumerate(heads):
        if prev is not None and b['level'] > prev + 1:
            add('warn', 'W_HEADING_JUMP', 0, f'标题层级从 h{prev} 跳到 h{b["level"]}：{b["text"][:30]}')
        prev = b['level']
    if levels and levels[0] != 1:
        add('warn', 'W_NO_H1', 0, f'首个标题是 h{levels[0]}，建议用一级标题作书名')

    # 自带目录
    _, had_toc = strip_generated_toc(list(lines))
    if had_toc:
        add('warn', 'W_TOC', 0, 'md 内含生成的目录块，构建时会自动剥离；如不需要可在 md 侧删除')

    # 段落硬换行：上一行不以句末标点结尾，下一行以汉字开头
    hardwrap = 0
    for i in range(len(lines) - 1):
        cur, nxt = lines[i].rstrip(), lines[i + 1].strip()
        if not cur or not nxt:
            continue
        if cur.startswith(('|', '#', '>')) or nxt.startswith(('|', '#', '>')):
            continue
        if not cur.endswith(tuple('。！？；：”』】）…')) and re.match(r'^[\u4e00-\u9fff]', nxt):
            hardwrap += 1
    if hardwrap > 3:
        add('warn', 'W_HARDWRAP', 0, f'疑似硬换行 {hardwrap} 处，转 EPUB 后段落会被拆碎')

    # 表格
    table_rows = [l for l in lines if l.startswith('|')]
    token = len(re.findall(r'<[a-z]{2,6}>', text))
    if token:
        add('error', 'E_TABLE_TOKEN', 0, f'残留 {token} 个表格结构标记（<fcel> 等），须在 md 侧复原')
    in_table, header_cols = False, None
    for no, line in enumerate(lines, 1):
        if line.startswith('|'):
            cols = line.strip().strip('|').count('|') + 1
            if not in_table:
                in_table, header_cols = True, cols
            elif cols != header_cols:
                add('warn', 'W_TABLE_COLS', no, f'表格列数 {cols} 与表头 {header_cols} 不一致')
        else:
            in_table, header_cols = False, None

    # 脚注
    # 圈号：行首、或同一行出现 3 个以上，都按正文列举处理，不算脚注角标
    body_marks = 0
    for l in lines:
        if l.startswith('>'):
            continue
        marks = [i for i, ch in enumerate(l) if ch in CIRCLED]
        # 角标一般出现在句末或紧跟标点；后面接空格/汉字的多是正文列举
        tail = [i for i in marks
                if i == len(l) - 1 or l[i + 1] in '，。；：、）】》”’！？']
        if tail:
            body_marks += len(tail)
    note_lines = [l for l in lines if l.startswith('>') and l.lstrip('> ').startswith(tuple(CIRCLED))]
    plain_quotes = []
    seen_body = False
    for b in blocks:
        if b['t'] == 'note':
            if seen_body and not b['text'].startswith(tuple(CIRCLED)):
                plain_quotes.append(b)
        elif b['t'] != 'h':
            seen_body = True
    if body_marks and not note_lines:
        add('warn', 'W_FOOTNOTE_ORPHAN', 0, f'正文有 {body_marks} 个角标但没有脚注行')
    if plain_quotes:
        add('warn', 'W_QUOTE', 0, f'{len(plain_quotes)} 行引用块不是脚注，将按普通引用渲染')

    # 图片
    for no, line in enumerate(lines, 1):
        for alt, src in IMG_RE.findall(line):
            if not alt.strip():
                add('warn', 'W_IMAGE_ALT', no, f'图片缺少替代文字：{src}')
            if re.search(r'[^\x00-\x7f]', src):
                add('warn', 'W_IMAGE_NAME', no, f'图片路径含非 ASCII 字符，部分阅读器会掉图：{src}')
            target = (p.parent / src).resolve()
            if not target.is_file():
                add('error', 'E_IMAGE_MISSING', no, f'图片文件不存在：{src}')

    # OCR/转换残留
    for no, line in enumerate(lines, 1):
        if re.match(r'^\s*\d{1,4}\s*$', line) or re.search(r'[|｜]\s*\d{1,4}\s*$', line.strip()):
            add('warn', 'W_PAGE_HEADER', no, f'疑似页眉/页码残留：{line.strip()[:40]}')
        if line.strip().startswith('<!--'):
            add('warn', 'W_COMMENT', no, 'HTML 注释会原样进入 EPUB')

    # 超长段落
    for no, line in enumerate(lines, 1):
        if len(line) > 2000 and not line.startswith(('|', '#', '>')):
            add('warn', 'W_LONG_PARA', no, f'单段 {len(line)} 字，可能多个段落被合并')

    errors = [f for f in findings if f['code'] in BLOCKING]
    warns = [f for f in findings if f['code'] not in BLOCKING]
    print(f'# MD 契约检查：{p.name}')
    print(f'- 行数 {len(lines)}，标题 {len(heads)} 个，表格行 {len(table_rows)} 行，脚注 {len(note_lines)} 条')
    print(f'- 阻断项 {len(errors)}，提醒项 {len(warns)}')
    for f in findings[:60]:
        flag = '×' if f['code'] in BLOCKING else '!'
        print(f'  {flag} [{f["code"]}] 第 {f["line"]} 行 {f["message"]}')
    if len(findings) > 60:
        print(f'  …… 另有 {len(findings) - 60} 条')
    return 1 if errors else 0


# ---------------------------------------------------------------- plan

def plan_md(md_path, write_config=''):
    p = Path(md_path).expanduser().resolve()
    lines = p.read_text(encoding='utf-8').split('\n')
    lines, _ = strip_generated_toc(lines)
    blocks = parse_blocks(lines)
    blocks = drop_title_block(blocks)

    levels = Counter(b['level'] for b in blocks if b['t'] == 'h')
    tables = [b for b in blocks if b['t'] == 'table']
    col_hist = Counter(max(0, b['rows'][0].strip().strip('|').count('|')) for b in tables)
    notes = sum(1 for b in blocks if b['t'] == 'note' and b['text'][:1] in CIRCLED)

    # 选分章层级：2—4 级里挑章数不过多的最深层
    def level_stats(level):
        sizes, titles, cur, title = [], [], 0, ''
        for b in blocks:
            if b['t'] == 'h' and b['level'] <= level:
                sizes.append(cur)
                titles.append(title)
                cur, title = 0, b['text']
            else:
                cur += block_size(b)
        sizes.append(cur)
        titles.append(title)
        return sizes, titles

    # 选分章层级：优先让单章落在 3000—15000 字，其次取最接近该区间的层级
    split, best = 1, None
    for level in (2, 3, 4):
        if not levels.get(level):
            continue
        sizes, _ = level_stats(level)
        vals = sorted(s for s in sizes if s > 0)
        if not vals or len(vals) > 300:
            continue
        med = vals[len(vals) // 2]
        if 3000 <= med <= 15000:
            score = 0
        elif med < 3000:
            score = 3000 - med
        else:
            score = med - 15000
        if best is None or score <= best[0]:
            best = (score, level)
    if best:
        split = best[1]

    sizes, titles = level_stats(split)
    pairs = [(s, t) for s, t in zip(sizes, titles) if s > 0]
    sizes_only = sorted(s for s, _ in pairs)
    median = sizes_only[len(sizes_only) // 2] if sizes_only else 0
    big = [(s, t) for s, t in pairs if s > 26000]

    print(f'# 转换规划：{p.name}')
    print(f'- 标题层级分布：{dict(sorted(levels.items()))}')
    print(f'- 表格 {len(tables)} 张，列数分布：{dict(sorted(col_hist.items()))}')
    print(f'- 脚注 {notes} 条；图片 {len(IMG_RE.findall(chr(10).join(lines)))} 张')
    print(f'- 按 split_level={split} 拟分章 {len(pairs)} 章，每章字数中位数 {median}，'
          f'最长 {sizes_only[-1] if sizes_only else 0}')
    if big:
        print(f'- 超长章 {len(big)} 个（>2.6 万字），将按段落续编：')
        for s, t in big[:8]:
            print(f'    {s:>7} 字  {t[:40]}')
    max_cols = max(col_hist) if col_hist else 0
    print(f'- 建议 split_level={split}；宽表卡片阈值建议 {max(7, min(8, max_cols)) if max_cols else 7} 列')

    if write_config:
        cfg = {
            'title': p.stem,
            'subtitle': '',
            'author': '',
            'publisher': '',
            'language': 'zh-CN',
            'date': '',
            'theme': 'literary',
            'split_level': split,
            'max_chars': 26000,
            'table_card_min_cols': 7,
            'table_rows_per_chunk': 30,
            'footnotes': 'endnote',
            'byline': True,
            'cover': {'accent': '#17324d', 'title_lines': []},
        }
        out = Path(write_config).expanduser().resolve()
        out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'- 已写入配置模板：{out}')
    return 0


# ---------------------------------------------------------------- CSS

THEMES = {
    'literary': """
body { margin: 0 2.2em; font-family: "Songti SC", "Source Han Serif SC", serif;
       line-height: 1.8; color: #1a1a1a; text-align: justify; }
p { text-indent: 2em; margin: 0.55em 0; }
h1 { font-size: 1.5em; margin: 2.2em 0 1.2em; text-align: center; letter-spacing: 0.08em; }
h2 { font-size: 1.2em; margin: 1.8em 0 0.9em; text-align: center; letter-spacing: 0.04em; }
h3 { font-size: 1.05em; margin: 1.5em 0 0.7em; font-weight: bold; }
h4 { font-size: 1em; margin: 1.2em 0 0.6em; font-weight: bold; color: #333; }
.part { margin: 3em 0 1.4em; text-align: center; font-size: 1.35em; letter-spacing: 0.12em; }
.byline { text-indent: 0; text-align: center; color: #666; font-size: 0.92em; margin: 0.6em 0 1.6em; }
h3.inner-part { text-align: center; font-size: 1.05em; letter-spacing: 0.28em; margin: 2em 0 1.1em; }
table { width: 100%; border-collapse: collapse; margin: 1em 0; font-size: 0.8em; line-height: 1.5; }
th, td { border: 1px solid #b5b5b5; padding: 0.32em 0.4em; vertical-align: top; text-align: left; }
th { background: #f1ece3; font-weight: bold; }
.card { margin: 1em 0; padding: 0.7em 0.85em; border: 1px solid #d9d3c7; background: #fbf9f4; }
.card p { text-indent: 0; margin: 0.25em 0; font-size: 0.9em; line-height: 1.6; }
.card .k { color: #7a5c3e; font-weight: bold; }
.notes { margin-top: 2em; border-top: 1px solid #d8d8d8; padding-top: 0.6em; }
.notes p { text-indent: 0; font-size: 0.82em; color: #555; line-height: 1.6; margin: 0.35em 0; }
sup a { color: #8b1e1e; text-decoration: none; }
figure { margin: 1.2em 0; text-align: center; }
img { max-width: 100%; height: auto; }
figcaption { font-size: 0.82em; color: #666; margin-top: 0.4em; text-indent: 0; }
.titlepage { margin-top: 22%; text-align: center; }
.titlepage .t { font-size: 2em; line-height: 1.5; letter-spacing: 0.1em; text-indent: 0; }
.titlepage .s { font-size: 1.15em; color: #555; margin-top: 0.8em; text-indent: 0; }
.titlepage .a { margin-top: 4em; font-size: 1.05em; text-indent: 0; }
.titlepage .p { margin-top: 0.6em; font-size: 0.9em; color: #777; text-indent: 0; }
""",
    'legal': """
body { margin: 0 2.2em; font-family: "Songti SC", "Source Han Serif SC", serif;
       line-height: 1.75; color: #1a1a1a; text-align: justify; }
p { text-indent: 2em; margin: 0.55em 0; }
h1 { font-size: 1.45em; margin: 1.6em 0 1.2em; text-align: center; letter-spacing: 0.06em; }
h2 { font-size: 1.2em; margin: 1.8em 0 0.9em; padding-left: 0.55em; border-left: 0.28em solid #8b1e1e; }
h3 { font-size: 1.06em; margin: 1.5em 0 0.7em; font-weight: bold; }
h4 { font-size: 1em; margin: 1.2em 0 0.6em; font-weight: bold; color: #3a3a3a; }
.part { margin: 2.6em 0 1.4em; text-align: center; font-size: 1.35em; letter-spacing: 0.08em; }
.byline { text-indent: 0; text-align: center; color: #666; font-size: 0.92em; margin: 0.6em 0 1.6em; }
h3.inner-part { text-align: center; font-size: 1.05em; letter-spacing: 0.28em; margin: 2em 0 1.1em; }
table { width: 100%; border-collapse: collapse; margin: 1em 0; font-size: 0.8em; line-height: 1.5; }
th, td { border: 1px solid #b5b5b5; padding: 0.32em 0.4em; vertical-align: top; text-align: left; }
th { background: #f1ece3; font-weight: bold; }
.card { margin: 1em 0; padding: 0.7em 0.85em; border: 1px solid #d9d3c7; background: #fbf9f4; }
.card p { text-indent: 0; margin: 0.25em 0; font-size: 0.9em; line-height: 1.6; }
.card .k { color: #7a5c3e; font-weight: bold; }
.notes { margin-top: 2em; border-top: 1px solid #d8d8d8; padding-top: 0.6em; }
.notes p { text-indent: 0; font-size: 0.82em; color: #555; line-height: 1.6; margin: 0.35em 0; }
sup a { color: #8b1e1e; text-decoration: none; }
figure { margin: 1.2em 0; text-align: center; }
img { max-width: 100%; height: auto; }
figcaption { font-size: 0.82em; color: #666; margin-top: 0.4em; text-indent: 0; }
.titlepage { margin-top: 22%; text-align: center; }
.titlepage .t { font-size: 2em; line-height: 1.5; letter-spacing: 0.1em; text-indent: 0; }
.titlepage .s { font-size: 1.15em; color: #555; margin-top: 0.8em; text-indent: 0; }
.titlepage .a { margin-top: 4em; font-size: 1.05em; text-indent: 0; }
.titlepage .p { margin-top: 0.6em; font-size: 0.9em; color: #777; text-indent: 0; }
""",
    'technical': """
body { margin: 0 1.8em; font-family: "PingFang SC", "Heiti SC", sans-serif;
       line-height: 1.7; color: #222; text-align: justify; }
p { text-indent: 0; margin: 0.5em 0; }
h1 { font-size: 1.5em; margin: 1.8em 0 1em; }
h2 { font-size: 1.22em; margin: 1.6em 0 0.8em; border-bottom: 1px solid #ddd; padding-bottom: 0.3em; }
h3 { font-size: 1.06em; margin: 1.4em 0 0.6em; font-weight: bold; }
h4 { font-size: 1em; margin: 1.1em 0 0.5em; font-weight: bold; color: #444; }
.part { margin: 2.2em 0 1.2em; font-size: 1.3em; font-weight: bold; }
.byline { text-indent: 0; color: #666; font-size: 0.9em; margin: 0.4em 0 1.4em; }
table { width: 100%; border-collapse: collapse; margin: 1em 0; font-size: 0.82em; }
th, td { border: 1px solid #ccc; padding: 0.3em 0.4em; vertical-align: top; }
th { background: #f4f4f4; }
.card { margin: 1em 0; padding: 0.6em 0.8em; border-left: 3px solid #999; background: #fafafa; }
.card p { text-indent: 0; margin: 0.2em 0; font-size: 0.9em; }
.card .k { font-weight: bold; color: #555; }
.notes { margin-top: 1.6em; border-top: 1px solid #ddd; padding-top: 0.5em; }
.notes p { text-indent: 0; font-size: 0.82em; color: #666; }
sup a { color: #3366cc; text-decoration: none; }
figure { margin: 1.2em 0; text-align: center; }
img { max-width: 100%; height: auto; }
figcaption { font-size: 0.82em; color: #666; margin-top: 0.4em; text-indent: 0; }
.titlepage { margin-top: 22%; text-align: center; }
.titlepage .t { font-size: 1.9em; text-indent: 0; }
.titlepage .s { font-size: 1.1em; color: #555; margin-top: 0.8em; text-indent: 0; }
.titlepage .a { margin-top: 4em; text-indent: 0; }
.titlepage .p { margin-top: 0.6em; font-size: 0.9em; color: #777; text-indent: 0; }
""",
}

COVER_CSS = """
.cover-page { margin: 0; padding: 0; text-align: center; }
.cover-page img { width: 100%; height: auto; display: block; }
"""


# ---------------------------------------------------------------- build

def split_chapters(blocks, split_level=3, max_chars=26000):
    chapters, cur = [], {'title': '', 'level': 0, 'blocks': []}
    for b in blocks:
        if b['t'] == 'h' and b['level'] <= split_level:
            if cur['blocks'] or cur['title']:
                chapters.append(cur)
            cur = {'title': b['text'], 'level': b['level'], 'blocks': []}
        else:
            cur['blocks'].append(b)
    if cur['blocks'] or cur['title']:
        chapters.append(cur)

    out = []
    for ch in chapters:
        out.extend(cut_chapter(ch, max_chars))
    for idx, ch in enumerate(out, 1):
        ch['id'] = f'c{idx:04d}'
    return out


def block_size(b):
    if b['t'] == 'table':
        return sum(len(r) for r in b['rows'])
    return len(b.get('text', ''))


def cut_chapter(ch, max_chars):
    if sum(block_size(b) for b in ch['blocks']) <= max_chars:
        return [ch]
    parts, cur, size = [], [], 0
    for b in ch['blocks']:
        n = block_size(b)
        if cur and size + n > max_chars:
            parts.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += n
    if cur:
        parts.append(cur)
    return [{'title': ch['title'] if i == 0 else f"{ch['title']}（续{i}）",
             'level': ch['level'] or 1, 'blocks': part}
            for i, part in enumerate(parts)]


def table_html(rows, card_min_cols=7):
    header = [c.strip() for c in rows[0].strip().strip('|').split('|')]
    body = []
    for r in rows[1:]:
        if re.fullmatch(r'\|[\s:\-|]+\|', r.strip()):
            continue
        body.append([c.strip() for c in r.strip().strip('|').split('|')])
    ncols = len(header)
    if ncols >= card_min_cols:
        out = []
        for row in body:
            cells = row[:ncols] + [''] * (ncols - len(row))
            lines = [f'<p><span class="k">{esc(name)}</span>　{esc(value)}</p>'
                     for name, value in zip(header, cells) if value]
            if lines:
                out.append('<div class="card">' + ''.join(lines) + '</div>')
        return '\n'.join(out) or '<p class="byline">（本表为空）</p>'
    head = ''.join(f'<th>{esc(c)}</th>' for c in header)
    rows_html = []
    for row in body:
        cells = row[:ncols] + [''] * (ncols - len(row))
        rows_html.append('<tr>' + ''.join(f'<td>{esc(c)}</td>' for c in cells) + '</tr>')
    return ('<table><thead><tr>' + head + '</tr></thead><tbody>'
            + ''.join(rows_html) + '</tbody></table>')


def render_chapter(ch, cfg, images):
    notes = [b['text'] for b in ch['blocks'] if b['t'] == 'note' and b['text'][:1] in CIRCLED]
    note_index, occurrences, seen, note_html = 0, {}, {}, []

    def link_marks(text):
        nonlocal note_index
        out = []
        for char in text:
            if char in CIRCLED and note_index < len(notes) and notes[note_index].startswith(char):
                note_index += 1
                body = notes[note_index - 1][1:].strip()
                if body in seen:
                    mark, nid = seen[body]
                else:
                    mark = CIRCLED[len(seen)] if len(seen) < len(CIRCLED) else f'[{len(seen) + 1}]'
                    nid = f"{ch['id']}n{len(seen) + 1}"
                    seen[body] = (mark, nid)
                    note_html.append(f'<p id="{nid}">{esc(mark)}　{esc(body)}'
                                     f' <a href="#r{nid}" style="text-decoration:none">↩</a></p>')
                occurrences[nid] = occurrences.get(nid, 0) + 1
                rid = f'r{nid}' if occurrences[nid] == 1 else f'r{nid}_{occurrences[nid]}'
                out.append(f'<sup><a id="{rid}" href="#{nid}">{esc(mark)}</a></sup>')
            else:
                out.append(esc(char))
        return ''.join(out)

    def render_text(text):
        """段落内处理图片与脚注标记。"""
        pieces, last = [], 0
        for m in IMG_RE.finditer(text):
            pieces.append(link_marks(text[last:m.start()]))
            alt, src = m.group(1), m.group(2)
            name = images.get(src)
            if name:
                # 「第 N 页图」这类占位 alt 只留作无障碍文字，不显示成图注
                show_caption = bool(alt) and not re.fullmatch(r'第\s*\d+\s*页图', alt.strip())
                pieces.append(f'<figure><img src="../images/{name}" alt="{esc(alt)}"/>'
                              + (f'<figcaption>{esc(alt)}</figcaption>' if show_caption else '')
                              + '</figure>')
            else:
                pieces.append(f'<p class="byline">［图片缺失：{esc(alt or src)}］</p>')
            last = m.end()
        pieces.append(link_marks(text[last:]))
        return ''.join(pieces)

    parts, prev = [], 'h'
    for b in ch['blocks']:
        if b['t'] == 'note':
            continue
        if b['t'] == 'table':
            parts.append(table_html(b['rows'], cfg['table_card_min_cols']))
            prev = 'table'
        elif b['t'] == 'h':
            tag = 'h3' if b['level'] == 4 else 'h4'
            parts.append(f'<{tag}>{esc(b["text"])}</{tag}>')
            prev = 'h'
        else:
            text = b['text']
            if cfg.get('byline', True) and prev == 'h' and 2 <= len(text) <= 12 \
                    and re.fullmatch(r'[\u4e00-\u9fff　 ]+', text):
                parts.append(f'<p class="byline">{esc(text)}</p>')
            elif re.fullmatch(r'第[一二三四五六七八九十]+部分', text):
                parts.append(f'<h3 class="inner-part">{esc(text)}</h3>')
            else:
                parts.append(f'<p>{render_text(text)}</p>')
            prev = 'p'
    for offset, note in enumerate(notes[note_index:]):
        num = note_index + offset + 1
        mark = CIRCLED[num - 1] if num <= len(CIRCLED) else f'[{num}]'
        note_html.append(f'<p id="{ch["id"]}x{num}">{esc(mark)}　{esc(note[1:].strip())}</p>')
    if note_html and cfg.get('footnotes', 'endnote') != 'off':
        parts.append('<div class="notes"><p style="text-indent:0;font-weight:bold">注释</p>'
                     + ''.join(note_html) + '</div>')
    return '\n'.join(parts)


def chapter_xhtml(ch, body, cfg):
    if ch['level'] <= 2:
        head = f'<h1 class="part">{esc(ch["title"])}</h1>'
    elif ch['level'] == 3:
        head = f'<h2>{esc(ch["title"])}</h2>'
    else:
        head = f'<h3>{esc(ch["title"])}</h3>'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{cfg['language']}" lang="{cfg['language']}">
<head><meta charset="utf-8"/><title>{esc(ch['title'] or '正文')}</title>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body><section epub:type="chapter">
{head}
{body}
</section></body></html>
"""


def nav_tree(chapters):
    """按标题层级建任意深度的目录树（支持 编 → 类别 → 法规 → 章）。"""
    root = {'children': []}
    stack = [(0, root)]
    for ch in chapters:
        node = {'title': ch['title'], 'href': f"text/{ch['id']}.xhtml",
                'level': ch['level'], 'children': []}
        while stack and stack[-1][0] >= ch['level']:
            stack.pop()
        stack[-1][1]['children'].append(node)
        stack.append((ch['level'], node))
    return root['children']


def render_nav_nodes(nodes):
    out = []
    for node in nodes:
        if node['children']:
            out.append(f'<li><a href="{node["href"]}">{esc(node["title"])}</a><ol>')
            out.extend(render_nav_nodes(node['children']))
            out.append('</ol></li>')
        else:
            out.append(f'<li><a href="{node["href"]}">{esc(node["title"])}</a></li>')
    return out


def nav_xhtml(cfg, chapters):
    items = ''.join(render_nav_nodes(nav_tree(chapters)))
    first = chapters[0]['id']
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{cfg['language']}" lang="{cfg['language']}">
<head><meta charset="utf-8"/><title>目录</title></head>
<body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>{items}</ol></nav>
<nav epub:type="landmarks" hidden="hidden"><ol>
<li><a epub:type="bodymatter" href="text/{first}.xhtml">正文</a></li>
</ol></nav></body></html>
"""


def ncx_xml(cfg, chapters, uid):
    points = ''.join(
        f'<navPoint id="np{i}" playOrder="{i}"><navLabel><text>{esc(c["title"])}</text></navLabel>'
        f'<content src="text/{c["id"]}.xhtml"/></navPoint>' for i, c in enumerate(chapters, 1))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="{cfg['language']}">
<head><meta name="dtb:uid" content="{uid}"/><meta name="dtb:depth" content="2"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>{esc(cfg['title'])}</text></docTitle><navMap>{points}</navMap></ncx>
"""


def opf_xml(cfg, chapters, uid, images):
    items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="titlepage" href="text/titlepage.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="cover"/>', '<itemref idref="titlepage"/>']
    if cfg.get('cover_file'):
        ext = Path(cfg['cover_file']).suffix.lower()
        items.append(f'<item id="cover-image" href="images/{cfg["cover_file"]}" '
                     f'media-type="{MEDIA_TYPES.get(ext, "image/png")}" '
                     f'properties="cover-image"/>')
    for i, (name, path) in enumerate(sorted(images.items())):
        items.append(f'<item id="img{i}" href="images/{path}" '
                     f'media-type="{MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")}"/>')
    for ch in chapters:
        items.append(f'<item id="{ch["id"]}" href="text/{ch["id"]}.xhtml" '
                     f'media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{ch["id"]}"/>')
    # EPUB2 兼容：部分阅读器（含微信读书部分版本）只认 guide 里的封面指引
    guide = ('<guide><reference type="cover" href="text/cover.xhtml" title="封面"/>'
             '<reference type="toc" href="nav.xhtml" title="目录"/></guide>')
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{cfg['language']}">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{uid}</dc:identifier>
<dc:title>{esc(cfg['title'])}</dc:title>
<dc:creator>{esc(cfg.get('author', ''))}</dc:creator>
<dc:publisher>{esc(cfg.get('publisher', ''))}</dc:publisher>
<dc:language>{cfg['language']}</dc:language>
<dc:date>{cfg.get('date', '')}</dc:date>
<meta property="dcterms:modified">{cfg['modified']}</meta>
<meta name="cover" content="cover-image"/>
</metadata>
<manifest>{''.join(items)}</manifest>
<spine toc="ncx">{''.join(spine)}</spine>
{guide}
</package>
"""


def cover_pages(cfg):
    title_lines = cfg.get('cover', {}).get('title_lines') or [cfg['title']]
    t = ''.join(f'<p class="t">{esc(x)}</p>' for x in title_lines)
    cover = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{cfg['language']}" lang="{cfg['language']}">
<head><meta charset="utf-8"/><title>封面</title>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body epub:type="cover"><div class="cover-page">
<img src="../images/{cfg['cover_file']}" alt="封面"/></div></body></html>
""" if cfg.get('cover_file') else f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{cfg['language']}" lang="{cfg['language']}">
<head><meta charset="utf-8"/><title>封面</title>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body><div class="titlepage">{t}<p class="s">{esc(cfg.get('subtitle', ''))}</p></div></body></html>
"""
    titlepage = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{cfg['language']}" lang="{cfg['language']}">
<head><meta charset="utf-8"/><title>书名页</title>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body><div class="titlepage">{t}
<p class="s">{esc(cfg.get('subtitle', ''))}</p>
<p class="a">{esc(cfg.get('author', ''))}</p>
<p class="p">{esc(cfg.get('publisher', ''))}</p></div></body></html>
"""
    return cover, titlepage


def make_cover_png(cfg, out_path):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    accent = cfg.get('cover', {}).get('accent', '#17324d')
    accent = accent.lstrip('#')
    rgb = tuple(int(accent[i:i + 2], 16) for i in (0, 2, 4))
    w, h = 1200, 1800
    img = Image.new('RGB', (w, h), rgb)
    draw = ImageDraw.Draw(img)
    fonts = [
        '/System/Library/Fonts/Supplemental/Songti.ttc',
        '/System/Library/Fonts/Songti.ttc',
        '/System/Library/Fonts/STHeiti Medium.ttc',
    ]
    font_path = next((f for f in fonts if Path(f).is_file()), None)
    if not font_path:
        return False
    title_font = ImageFont.truetype(font_path, 96, index=1)
    sub_font = ImageFont.truetype(font_path, 46, index=1)
    small = ImageFont.truetype(font_path, 34, index=0)
    draw.rectangle([60, 60, w - 60, h - 60], outline=(235, 226, 208), width=3)
    y = 420
    for line in cfg.get('cover', {}).get('title_lines') or [cfg['title']]:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        draw.text(((w - (bbox[2] - bbox[0])) / 2, y), line, font=title_font, fill=(246, 241, 231))
        y += 132
    if cfg.get('subtitle'):
        bbox = draw.textbbox((0, 0), cfg['subtitle'], font=sub_font)
        draw.text(((w - (bbox[2] - bbox[0])) / 2, y + 40), cfg['subtitle'],
                  font=sub_font, fill=(221, 210, 190))
        y += 106
    draw.line([(w / 2 - 90, y + 70), (w / 2 + 90, y + 70)], fill=(200, 188, 166), width=2)
    for text, dy, color in ((cfg.get('author', ''), 400, (233, 224, 205)),
                            (cfg.get('publisher', ''), 330, (196, 184, 164))):
        if not text:
            continue
        bbox = draw.textbbox((0, 0), text, font=small)
        draw.text(((w - (bbox[2] - bbox[0])) / 2, h - dy), text, font=small, fill=color)
    img.save(out_path, 'PNG')
    return True


def prepare_cover(src, dst, max_side=1280, quality=84):
    """把封面统一成阅读器友好的 JPEG（RGB、baseline、72dpi、≤1280px、~150KB）。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    with Image.open(src) as img:
        img = img.convert('RGB')
        w, h = img.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        img.save(dst, 'JPEG', quality=quality, optimize=True, dpi=(72, 72))
    return True


def trim_uniform(img, tol=14, pad=0.008):
    """按四角底色裁掉扫描白边/黑边。"""
    from PIL import Image, ImageChops
    w, h = img.size
    bg = Image.new('RGB', img.size, img.getpixel((w - 1, h - 1)))
    diff = ImageChops.difference(img, bg).convert('L').point(lambda v: 255 if v > tol else 0)
    box = diff.getbbox()
    if not box:
        return img
    px, py = int(w * pad), int(h * pad)
    left, top = max(0, box[0] - px), max(0, box[1] - py)
    right, bottom = min(w, box[2] + px), min(h, box[3] + py)
    if (right - left) < w * 0.5 or (bottom - top) < h * 0.5:
        return img
    return img.crop((left, top, right, bottom))


def edge_report(img, band=0.02):
    """打印四条边与内部的亮度差，给出 --crop 建议（装订边/扫描边靠这个定）。"""
    g = img.convert('L')
    w, h = g.size
    px = g.load()

    def mean_col(x):
        ys = range(0, h, 5)
        return sum(px[x, y] for y in ys) / len(list(ys))

    def mean_row(y):
        xs = range(0, w, 5)
        return sum(px[x, y] for x in xs) / len(list(xs))

    inner = (mean_col(int(w * 0.3)) + mean_col(int(w * 0.7))) / 2
    left = sum(mean_col(x) for x in range(int(w * band))) / max(1, int(w * band))
    right = sum(mean_col(w - 1 - x) for x in range(int(w * band))) / max(1, int(w * band))
    top = sum(mean_row(y) for y in range(int(h * band))) / max(1, int(h * band))
    bottom = sum(mean_row(h - 1 - y) for y in range(int(h * band))) / max(1, int(h * band))
    diffs = {'左': left - inner, '右': right - inner, '上': top - inner, '下': bottom - inner}
    hot = {k: v for k, v in diffs.items() if abs(v) > 6}
    if hot:
        hint = ', '.join(f'{k}{v:+.0f}' for k, v in hot.items())
        print(f'  边缘亮度差：{hint}（内部 {inner:.0f}）；'
              f'若确为装订边，可用 --crop 按边裁 2%—5% 再试')
    return diffs


def auto_crop_edges(img, max_frac=0.08, tol=20):
    """裁掉与内部明显不同色的窄边条（装订白边、扫描黑边）。

    与 trim_uniform 的区别：不假设边色等于底色，而是逐行/列比较亮度差的连续性。
    """
    g = img.convert('L')
    w, h = g.size
    px = g.load()

    def col_mean(x):
        ys = range(0, h, 5)
        return sum(px[x, y] for y in ys) / len(list(ys))

    def row_mean(y):
        xs = range(0, w, 5)
        return sum(px[x, y] for x in xs) / len(list(xs))

    left = right = top = bottom = 0
    inner = col_mean(int(w * 0.2))
    for x in range(int(w * max_frac)):
        if abs(col_mean(x) - inner) > tol:
            left = x + 1
        else:
            break
    inner = col_mean(int(w * 0.8))
    for x in range(int(w * max_frac)):
        if abs(col_mean(w - 1 - x) - inner) > tol:
            right = x + 1
        else:
            break
    inner = row_mean(int(h * 0.2))
    for y in range(int(h * max_frac)):
        if abs(row_mean(y) - inner) > tol:
            top = y + 1
        else:
            break
    inner = row_mean(int(h * 0.8))
    for y in range(int(h * max_frac)):
        if abs(row_mean(h - 1 - y) - inner) > tol:
            bottom = y + 1
        else:
            break

    if not any((left, right, top, bottom)):
        return img
    return img.crop((left, top, w - right, h - bottom))


def extract_cover(pdf_path, out_path, page=0, dpi=300, crop='', trim=False,
                  max_side=1600, quality=88):
    """从 PDF 指定页渲染封面（渲染而非抽内嵌图，避免反色/掩膜问题）。"""
    try:
        import fitz
    except ImportError:
        raise SystemExit('需要 PyMuPDF：python3 -m pip install pymupdf')
    import io
    from PIL import Image

    doc = fitz.open(Path(pdf_path).expanduser().resolve())
    if not 0 <= page < doc.page_count:
        raise SystemExit(f'页码超出范围：{page}（共 {doc.page_count} 页）')
    pix = doc[page].get_pixmap(dpi=dpi)
    if pix.n > 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    img = Image.open(io.BytesIO(pix.tobytes('png'))).convert('RGB')
    edge_report(img)

    if crop:
        vals = [float(x) for x in crop.split(',')]
        if len(vals) != 4:
            raise SystemExit('--crop 需要四个百分比，如 0.02,0,0,0')
        w, h = img.size
        left, top, right, bottom = (int(w * vals[0]), int(h * vals[1]),
                                    int(w * vals[2]), int(h * vals[3]))
        img = img.crop((left, top, w - right, h - bottom))

    if trim:
        img = trim_uniform(img)
        img = auto_crop_edges(img)

    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, 'JPEG', quality=quality, optimize=True, dpi=(72, 72))
    gray = img.convert('L')
    lum = sum(gray.getdata()) / (gray.width * gray.height)
    note = '（偏暗，微信读书书架缩略图可能看不出内容）' if lum < 40 else ''
    print(f'封面已导出：{out}  {img.size[0]}x{img.size[1]}  {out.stat().st_size // 1024} KB  '
          f'平均亮度 {lum:.0f}{note}')
    return str(out)


def build_epub(md_path, cfg_path, out_path='', preview='', cover_path='', id_suffix=''):
    md = Path(md_path).expanduser().resolve()
    cfg_file = Path(cfg_path).expanduser().resolve()
    cfg = json.loads(cfg_file.read_text(encoding='utf-8'))
    cfg.setdefault('language', 'zh-CN')
    cfg.setdefault('theme', 'literary')
    cfg.setdefault('split_level', 3)
    cfg.setdefault('max_chars', 26000)
    cfg.setdefault('table_card_min_cols', 7)
    cfg.setdefault('table_rows_per_chunk', 30)
    cfg.setdefault('footnotes', 'endnote')
    cfg.setdefault('byline', True)
    cfg.setdefault('cover', {})
    if id_suffix:
        cfg['id_suffix'] = id_suffix
    cfg['modified'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    lines = md.read_text(encoding='utf-8').split('\n')
    lines, _ = strip_generated_toc(lines)
    blocks = parse_blocks(lines, cfg['table_rows_per_chunk'])
    # 配置没写书名/副标题时，从 md 的一级标题与首条引用里补
    if not cfg.get('title'):
        cfg['title'] = next((b['text'] for b in blocks
                             if b['t'] == 'h' and b['level'] == 1), md.stem)
    if not cfg.get('subtitle'):
        head_blocks = [b for b in blocks[:6] if b['t'] == 'note'
                       and not b['text'].startswith(tuple(CIRCLED))]
        cfg['subtitle'] = head_blocks[0]['text'] if head_blocks else ''
    blocks = drop_title_block(blocks)

    # 图片：复制到包内，文件名转 ASCII 小写，保持映射
    images = {}
    used = set()
    for b in blocks:
        if b['t'] != 'p':
            continue
        for _, src in IMG_RE.findall(b['text']):
            if src in images or src.startswith(('http://', 'https://', 'data:')):
                continue
            source = (md.parent / src).resolve()
            if not source.is_file():
                continue
            name = slug_image(source.name, used)
            images[src] = name

    chapters = split_chapters(blocks, cfg['split_level'], cfg['max_chars'])
    if not chapters:
        raise SystemExit('没有可转换的章节内容')

    out = Path(out_path).expanduser().resolve() if out_path else md.with_suffix('.epub')
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / f'.{out.stem}.build'
    if work.exists():
        shutil.rmtree(work)
    (work / 'OEBPS' / 'text').mkdir(parents=True)
    (work / 'OEBPS' / 'images').mkdir(parents=True)
    (work / 'META-INF').mkdir(parents=True)

    cover_src = cover_path or cfg.get('cover', {}).get('image')
    cfg['cover_file'] = ''
    if cover_src:
        src_cover = Path(cover_src).expanduser()
        if not src_cover.is_absolute():
            src_cover = (cfg_file.parent / src_cover).resolve()
        if src_cover.is_file():
            target = work / 'OEBPS' / 'images' / 'cover.jpg'
            if prepare_cover(src_cover, target):
                cfg['cover_file'] = 'cover.jpg'
            else:
                fallback = work / 'OEBPS' / 'images' / f'cover{src_cover.suffix.lower()}'
                shutil.copyfile(src_cover, fallback)
                cfg['cover_file'] = fallback.name
        else:
            print(f'封面文件不存在，改为生成封面：{src_cover}', file=sys.stderr)
    if not cfg['cover_file']:
        generated = work / 'OEBPS' / 'images' / 'cover.png'
        if make_cover_png(cfg, generated):
            cfg['cover_file'] = 'cover.png'
        else:
            generated.unlink(missing_ok=True)

    for src, name in images.items():
        shutil.copyfile((md.parent / src).resolve(), work / 'OEBPS' / 'images' / name)

    uid_seed = cfg['title'] + cfg.get('author', '') + (cfg.get('id_suffix') or '')
    uid = 'urn:uuid:' + str(uuid.uuid5(uuid.NAMESPACE_URL, uid_seed))
    chapter_files = {}
    for ch in chapters:
        chapter_files[ch['id']] = chapter_xhtml(ch, render_chapter(ch, cfg, images), cfg)

    (work / 'OEBPS' / 'style.css').write_text(
        THEMES.get(cfg['theme'], THEMES['literary']) + COVER_CSS, encoding='utf-8')
    (work / 'OEBPS' / 'nav.xhtml').write_text(nav_xhtml(cfg, chapters), encoding='utf-8')
    (work / 'OEBPS' / 'toc.ncx').write_text(ncx_xml(cfg, chapters, uid), encoding='utf-8')
    (work / 'OEBPS' / 'content.opf').write_text(opf_xml(cfg, chapters, uid, images), encoding='utf-8')
    cover, titlepage = cover_pages(cfg)
    (work / 'OEBPS' / 'text' / 'cover.xhtml').write_text(cover, encoding='utf-8')
    (work / 'OEBPS' / 'text' / 'titlepage.xhtml').write_text(titlepage, encoding='utf-8')
    for cid, content in chapter_files.items():
        (work / 'OEBPS' / 'text' / f'{cid}.xhtml').write_text(content, encoding='utf-8')
    (work / 'META-INF' / 'container.xml').write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>\n', encoding='utf-8')

    with zipfile.ZipFile(out, 'w') as zf:
        zf.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        for path in sorted(work.rglob('*')):
            if path.is_file():
                rel = path.relative_to(work).as_posix()
                zf.write(path, rel, compress_type=zipfile.ZIP_DEFLATED)
    shutil.rmtree(work)

    print(f'生成 {out}  {len(chapters)} 章  {out.stat().st_size / 1024:.0f} KB  '
          f'图片 {len(images) + (1 if cfg.get("cover_file") else 0)} 张  '
          f'主题 {cfg["theme"]}')
    if preview:
        preview_dir = Path(preview).expanduser().resolve()
        preview_dir.mkdir(parents=True, exist_ok=True)
        make_preview(out, preview_dir)
    return str(out)


def make_preview(epub_path, out_dir):
    chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    if not Path(chrome).is_file():
        print('未找到 Chrome，跳过预览截图', file=sys.stderr)
        return
    tmp = out_dir / '_unpack'
    if tmp.exists():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(epub_path) as zf:
        zf.extractall(tmp)
    targets = sorted((tmp / 'OEBPS' / 'text').glob('*.xhtml'))[:3]
    for i, target in enumerate(targets):
        png = out_dir / f'preview{i + 1}.png'
        subprocess.run([chrome, '--headless=new', '--disable-gpu', '--hide-scrollbars',
                        '--window-size=620,1000', f'--screenshot={png}', f'file://{target}'],
                       check=False, capture_output=True)
        if png.is_file():
            print(f'预览图 {png}')
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- check

def check_epub(epub_path):
    path = Path(epub_path).expanduser().resolve()
    fails, warns = [], []

    def fail(code, msg):
        fails.append((code, msg))

    def warn(code, msg):
        warns.append((code, msg))

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        name_set = set(names)
        info = zf.infolist()

        # 1. mimetype
        if not names or names[0] != 'mimetype':
            fail('E_MIMETYPE_ORDER', 'mimetype 不是压缩包第一项')
        if 'mimetype' in name_set:
            if info[0].compress_type != zipfile.ZIP_STORED:
                fail('E_MIMETYPE_STORED', 'mimetype 被压缩，必须 ZIP_STORED')
            if zf.read('mimetype').decode('ascii', 'replace') != 'application/epub+zip':
                fail('E_MIMETYPE_CONTENT', 'mimetype 内容不是 application/epub+zip')

        # 2. container
        if 'META-INF/container.xml' not in name_set:
            fail('E_NO_CONTAINER', '缺少 META-INF/container.xml')
            return report(path, fails, warns)
        container = zf.read('META-INF/container.xml').decode('utf-8')
        m = re.search(r'full-path="([^"]+)"', container)
        if not m:
            fail('E_NO_ROOTFILE', 'container.xml 未声明 rootfile')
            return report(path, fails, warns)
        opf_path = m.group(1)
        if opf_path not in name_set:
            fail('E_NO_OPF', f'OPF 不存在：{opf_path}')
            return report(path, fails, warns)

        opf = zf.read(opf_path).decode('utf-8')
        base = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''

        # 3. 元数据
        if not re.search(r'<dc:identifier[^>]*id="([^"]+)"', opf):
            fail('E_NO_IDENTIFIER', '缺少 dc:identifier')
        m_uid = re.search(r'unique-identifier="([^"]+)"', opf)
        if not m_uid:
            fail('E_NO_UNIQUE_ID', 'package 缺少 unique-identifier 属性')
        elif not re.search(rf'id="{re.escape(m_uid.group(1))}"', opf):
            fail('E_ID_MISMATCH', 'unique-identifier 指向的 id 不存在')
        if 'dcterms:modified' not in opf:
            fail('E_NO_MODIFIED', '缺少 dcterms:modified（EPUB3 必需）')
        if not re.search(r'<dc:title>', opf):
            warn('W_NO_TITLE', '缺少 dc:title')
        if 'xml:lang' not in opf and '<dc:language>' not in opf:
            warn('W_NO_LANG', '缺少语言声明')

        # 4. manifest / spine
        manifest = dict(re.findall(r'<item[^>]*id="([^"]+)"[^>]*href="([^"]+)"', opf))
        manifest.update({i: h for h, i in re.findall(r'<item[^>]*href="([^"]+)"[^>]*id="([^"]+)"', opf)})
        if not manifest:
            fail('E_NO_MANIFEST', 'manifest 为空或无法解析')
        hrefs = set(manifest.values())
        for href in hrefs:
            if href.startswith(('http://', 'https://')):
                fail('E_REMOTE_RESOURCE', f'manifest 引用了远程资源：{href}')
                continue
            if base + href not in name_set:
                fail('E_MANIFEST_MISSING', f'manifest 声明的文件不存在：{href}')

        spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
        if not spine:
            fail('E_NO_SPINE', 'spine 为空')
        ids = set(re.findall(r'<item[^>]*id="([^"]+)"', opf))
        for ref in spine:
            if ref not in ids:
                fail('E_SPINE_ID', f'spine 引用了不存在的 id：{ref}')

        # 5. 未入清单的文件
        declared = {base + h for h in hrefs}
        for name in names:
            if name.endswith('/'):
                continue
            if name in ('mimetype',) or name.startswith('META-INF/') or name == opf_path:
                continue
            if name not in declared:
                warn('W_UNDECLARED', f'包内文件未在 manifest 声明：{name}')

        # 6. 导航
        nav_items = [i for i in re.findall(r'<item[^>]*>', opf) if 'properties="nav"' in i]
        if not nav_items:
            fail('E_NO_NAV', '缺少 EPUB3 导航文档（properties="nav"）')

        # 6.5 图片体积、清晰度与封面亮度（掉图/书架显示的经验性检查）
        if path.stat().st_size > 50 * 1024 * 1024:
            warn('W_BIG_BOOK', f'整包 {path.stat().st_size / 1048576:.0f} MB，超过 50 MB，'
                               f'部分平台上传会失败或转码很慢')
        cover_href = ''
        m = re.search(r'<item[^>]*href="([^"]+)"[^>]*properties="[^"]*cover-image', opf) \
            or re.search(r'<item[^>]*properties="[^"]*cover-image[^"]*"[^>]*href="([^"]+)"', opf)
        if m:
            cover_href = base + m.group(1)
        try:
            import io
            from PIL import Image
            for name in names:
                if not name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')):
                    continue
                data = zf.read(name)
                if len(data) > 2 * 1024 * 1024:
                    warn('W_IMG_LARGE', f'{name} {len(data) / 1048576:.1f} MB，建议压到 2 MB 以内')
                with Image.open(io.BytesIO(data)) as im:
                    w, h = im.size
                    gray = im.convert('L')
                    lum = sum(gray.resize((60, 60)).getdata()) / 3600
                if max(w, h) < 400:
                    warn('W_IMG_SMALL', f'{name} 只有 {w}x{h}，放大后会糊')
                if name == cover_href and lum < 40:
                    warn('W_COVER_DARK', f'封面平均亮度 {lum:.0f}/255，偏暗；'
                                         f'微信读书等平台的书架缩略图可能看不出内容')
        except ImportError:
            pass


        # 7. 逐个 XHTML 检查
        media_by_ext = MEDIA_TYPES
        for name in names:
            if not name.endswith('.xhtml'):
                continue
            try:
                import xml.dom.minidom as minidom
                minidom.parseString(zf.read(name))
            except Exception as exc:  # noqa: BLE001
                fail('E_XML', f'{name} 不是合法 XML：{exc}')
                continue
            doc = zf.read(name).decode('utf-8')
            dir_name = name.rsplit('/', 1)[0]

            for attr, ref in re.findall(r'(href|src)="([^"]+)"', doc):
                if ref.startswith(('http://', 'https://', 'mailto:', 'data:', '#')):
                    if ref.startswith(('http://', 'https://')):
                        warn('W_REMOTE_LINK', f'{name} 含外部链接：{ref[:60]}')
                    continue
                target = os.path.normpath(os.path.join(dir_name, ref.split('#')[0]))
                if target not in name_set:
                    if attr == 'src':
                        fail('E_IMAGE_BROKEN', f'{name} 引用的资源不存在（注意大小写）：{ref}')
                    else:
                        fail('E_LINK_BROKEN', f'{name} 内部链接失效：{ref}')

            for m in re.finditer(r'<img\b[^>]*>', doc):
                tag = m.group(0)
                if 'alt=' not in tag:
                    warn('W_IMG_ALT', f'{name} 有图片缺 alt：{tag[:60]}')
                src = re.search(r'src="([^"]+)"', tag)
                if src:
                    ext = Path(src.group(1)).suffix.lower()
                    if ext not in media_by_ext:
                        fail('E_IMG_FORMAT', f'{name} 图片格式不在核心媒体类型内：{src.group(1)}')

            for m in re.finditer(r'<image\b[^>]*xlink:href="([^"]+)"', doc):
                warn('W_SVG_IMAGE', f'{name} 用 SVG 嵌位图，部分阅读器不渲染：{m.group(1)}')

            if doc.count('<sup><a id="') and 'class="notes"' not in doc:
                warn('W_NOTE_TARGET', f'{name} 有脚注角标但无注释区')

    return report(path, fails, warns)


def report(path, fails, warns):
    print(f'# EPUB 校验：{path.name}')
    print(f'- 大小 {path.stat().st_size / 1024:.0f} KB')
    print(f'- 阻断项 {len(fails)}，提醒项 {len(warns)}')
    for code, msg in fails:
        print(f'  × [{code}] {msg}')
    for code, msg in warns[:40]:
        print(f'  ! [{code}] {msg}')
    if len(warns) > 40:
        print(f'  …… 另有 {len(warns) - 40} 条提醒')
    print('- 结论：' + ('未通过' if fails else '通过（结构化 + 资源引用）'))
    return 1 if fails else 0


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(prog='md2epub')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_lint = sub.add_parser('lint', help='按 MD 输入契约检查')
    p_lint.add_argument('md')

    p_plan = sub.add_parser('plan', help='统计结构并给出转换参数建议')
    p_plan.add_argument('md')
    p_plan.add_argument('--write-config', default='')

    p_build = sub.add_parser('build', help='生成 EPUB')
    p_build.add_argument('md')
    p_build.add_argument('--config', required=True)
    p_build.add_argument('--out', default='')
    p_build.add_argument('--preview', default='')
    p_build.add_argument('--cover', default='', help='指定封面图片（优先于配置）')
    p_build.add_argument('--id-suffix', default='', help='换一个书籍标识（用于绕开阅读器缓存，重传时相当于新书）')
    p_build.add_argument('--no-check', action='store_true')

    p_check = sub.add_parser('check', help='校验 EPUB')
    p_check.add_argument('epub')

    p_cover = sub.add_parser('cover', help='从 PDF 提取原书封面')
    p_cover.add_argument('pdf')
    p_cover.add_argument('--out', required=True)
    p_cover.add_argument('--page', type=int, default=0)
    p_cover.add_argument('--dpi', type=int, default=300)
    p_cover.add_argument('--crop', default='', help='左,上,右,下 各裁掉百分比，如 0.02,0,0,0')
    p_cover.add_argument('--trim', action='store_true', help='自动裁掉均匀白边/黑边')
    p_cover.add_argument('--max-side', type=int, default=1600)
    p_cover.add_argument('--quality', type=int, default=88)

    args = ap.parse_args()
    if args.cmd == 'lint':
        return lint_md(args.md)
    if args.cmd == 'plan':
        return plan_md(args.md, args.write_config)
    if args.cmd == 'cover':
        extract_cover(args.pdf, args.out, args.page, args.dpi, args.crop,
                      args.trim, args.max_side, args.quality)
        return 0
    if args.cmd == 'build':
        out = build_epub(args.md, args.config, args.out, args.preview, args.cover,
                         args.id_suffix)
        if not args.no_check:
            return check_epub(out)
        return 0
    if args.cmd == 'check':
        return check_epub(args.epub)
    return 0


if __name__ == '__main__':
    sys.exit(main())
