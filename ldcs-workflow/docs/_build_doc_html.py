#!/usr/bin/env python3
"""把 docs/<文档名>.md 渲染成可独立打开、可分享的 HTML（浅色单列布局）。

用法：python _build_doc_html.py [文档名，不带后缀]   默认「通用业务流程编排方案」

图片处理：md 里写 `![alt](assets/xxx.svg)`，构建时**把 SVG 内联进 HTML**，
所以产物是单文件、离线也能看（不依赖外部图片路径）。
"""
import re
import sys
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
ASSETS = DOCS / "assets"
STEM = sys.argv[1] if len(sys.argv) > 1 else "通用业务流程编排方案"
SRC = DOCS / f"{STEM}.md"
DST = DOCS / f"{STEM}.html"

RAW_MD = SRC.read_text(encoding="utf-8")

# 页头三行从 md 里取：H1 作主标题，H1 后第一段作副标题（都去掉 markdown 标记）
_lines = RAW_MD.splitlines()
_h1 = next((i for i, l in enumerate(_lines) if l.startswith("# ")), None)
TITLE = re.sub(r"[*`]", "", _lines[_h1][2:].strip()) if _h1 is not None else STEM
LEAD = ""
if _h1 is not None:
    for _l in _lines[_h1 + 1:]:
        _s = _l.strip()
        if _s and not _s.startswith(("#", "|", ">", "!", "-", "*", "`", "    ")):
            LEAD = re.sub(r"[*`]", "", _s)
            break

md = markdown.Markdown(
    extensions=["tables", "fenced_code", "toc", "sane_lists"],
    extension_configs={"toc": {"toc_depth": "2-3", "anchorlink": False}},
)
# 页头右上角那枚小标签，按文档性质给：需求书不是设计方案，别混
EYEBROW = "业务需求书" if "需求书" in STEM else "设计方案 · v1"
body = md.convert(RAW_MD)
toc = md.toc


def _inline(m):
    alt, src = m.group(1), m.group(2)
    name = src.split("/")[-1]
    path = ASSETS / name
    if not path.exists():
        return m.group(0)
    raw = path.read_text(encoding="utf-8")
    svg = re.sub(r'\s+width="\d+"\s+height="\d+"', "", raw, count=1)
    # 「放大到 100%」时的视口尺寸**从 SVG 自身取**，不要写死 ——
    # 画布会随内容增减而变，写死会在放大视图里裁掉底部。
    _vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', raw)
    vb_w = float(_vb.group(1)) if _vb else 0
    # 宽图（拓扑图，或画布本身很宽）默认缩到纸面宽看全貌，可一键放大到 100% 拖动细看
    if "拓扑" in name or vb_w > 1000:
        if _vb:
            ZOOM_DIM["w"], ZOOM_DIM["h"] = float(_vb.group(1)), float(_vb.group(2))
        return (
            '<figure class="fig wide">'
            '<input type="checkbox" id="zoomflow" class="zt">'
            '<div class="hintbar">'
            '<span>默认缩到整页宽看全貌，放大后可左右拖动细看</span>'
            '<label for="zoomflow" class="zlab"><span class="z1">放大到 100% 查看 ＋</span>'
            '<span class="z2">缩回整页查看 －</span></label>'
            f'<a href="assets/{name}" target="_blank" rel="noopener">在新标签页打开原图 ↗</a></div>'
            f'<div class="scroll">{svg}</div></figure>'
        )
    return f'<figure class="fig fit">{svg}</figure>'


ZOOM_DIM = {"w": 1842, "h": 1224}     # 拓扑图放大视图的视口尺寸（构建时被真实 viewBox 覆盖）

body = re.sub(r'<img\s+alt="([^"]*)"\s+src="([^"]*)"\s*/?>', _inline, body)

CSS = """
:root{
  --ink:#1a1d21; --ink-2:#3d444d; --muted:#6b7480; --line:#e6e9ee;
  --bg:#f7f8fa; --card:#ffffff; --accent:#1a5fb4; --accent-soft:#eaf2fd;
  --code-bg:#f4f6f8;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
              "Microsoft YaHei","Helvetica Neue",Arial,sans-serif;
  font-size:15px; line-height:1.85;
}
.wrap{max-width:1180px;margin:0 auto;padding:0 28px 96px;display:grid;
      grid-template-columns:248px minmax(0,1fr);gap:44px;align-items:start}
header.top{grid-column:1/-1;padding:54px 0 26px;border-bottom:1px solid var(--line);margin-bottom:34px}
header.top .eyebrow{color:var(--accent);font-size:12.5px;letter-spacing:.14em;font-weight:700;
                    text-transform:uppercase;margin-bottom:12px}
header.top h1{font-size:31px;line-height:1.35;margin:0 0 14px;letter-spacing:-.01em;font-weight:700}
header.top p.lead{color:var(--muted);font-size:14px;margin:0}
nav.toc{position:sticky;top:24px;font-size:13.5px;max-height:calc(100vh - 60px);
        overflow-y:auto;padding-right:10px;border-right:1px solid var(--line)}
nav.toc .toc-title{font-weight:700;font-size:11.5px;letter-spacing:.1em;color:var(--muted);
                   text-transform:uppercase;margin-bottom:12px}
nav.toc ul{list-style:none;margin:0;padding:0}
nav.toc li{margin:0}
nav.toc a{display:block;color:var(--ink-2);text-decoration:none;padding:5px 8px 5px 12px;
          border-left:2px solid transparent;border-radius:0 4px 4px 0;line-height:1.5;font-size:13.5px}
nav.toc a:hover{color:var(--accent);background:var(--accent-soft);border-left-color:var(--accent)}
nav.toc ul ul a{padding-left:26px;color:var(--muted);font-size:12.8px}
nav.toc ul ul{margin-top:2px;margin-bottom:4px}
main{background:var(--card);border:1px solid var(--line);border-radius:14px;
     padding:44px 48px 52px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 10px 32px rgba(16,24,40,.05);
     min-width:0}
main > h2{font-size:22px;margin:52px 0 16px;padding-bottom:11px;border-bottom:1px solid var(--line);
          font-weight:700;letter-spacing:-.01em}
main > h2:first-child{margin-top:0}
main > h3{font-size:17px;margin:34px 0 12px;font-weight:700}
main > h4{font-size:15px;margin:24px 0 10px;font-weight:700;color:var(--ink-2)}
p{margin:0 0 14px}
a{color:var(--accent)}
strong{font-weight:700;color:var(--ink)}
hr{border:0;border-top:1px solid var(--line);margin:38px 0}
blockquote{margin:0 0 16px;padding:12px 18px;background:var(--accent-soft);
           border-left:3px solid var(--accent);border-radius:0 8px 8px 0;color:var(--ink-2);font-size:14px}
blockquote p:last-child{margin-bottom:0}
table{border-collapse:separate;border-spacing:0;width:100%;margin:16px 0 22px;font-size:13.8px;
      border:1px solid var(--line);border-radius:10px;overflow:hidden}
th{background:#fafbfc;text-align:left;font-weight:700;color:var(--ink-2);
   padding:11px 14px;border-bottom:1px solid var(--line)}
td{padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:top;color:var(--ink-2)}
tr:last-child td{border-bottom:0}
tbody tr:hover td{background:#fcfdfe}
code{font-family:"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;font-size:12.8px;
     background:var(--code-bg);padding:2px 6px;border-radius:5px;color:#0b3d91;
     border:1px solid rgba(0,0,0,.045)}
pre{background:var(--code-bg);border:1px solid var(--line);border-radius:10px;
    padding:16px 18px;overflow-x:auto;margin:16px 0 22px;line-height:1.65}
pre code{background:none;border:0;padding:0;font-size:12.5px;color:#1f2733;white-space:pre}
ul,ol{margin:0 0 16px;padding-left:24px}
li{margin-bottom:7px}
li > ul,li > ol{margin-top:7px;margin-bottom:0}
figure{margin:22px 0 8px}
figure svg{display:block}
figure.fit,figure.wide{margin-left:-48px;margin-right:-48px}   /* 出血到卡片整宽，图不缩太小 */
figure.fit svg{width:100%;height:auto}
figure.wide .scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:#fff;
                    box-shadow:0 1px 2px rgba(16,24,40,.04)}
figure.wide .scroll svg{width:100%;height:auto}
figure.wide .zt{display:none}
figure.wide .zt:checked ~ .scroll svg{width:__ZW__px;height:__ZH__px;max-width:none}
.hintbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
         font-size:12.5px;color:var(--muted);margin:0 0 8px}
.hintbar a{color:var(--accent);text-decoration:none;font-weight:600}
.hintbar a:hover{text-decoration:underline}
.zlab{color:var(--accent);font-weight:600;cursor:pointer;user-select:none;
      border:1px solid #cfe0f5;background:var(--accent-soft);border-radius:999px;padding:3px 12px}
.zlab:hover{background:#dfebfb}
.zt:checked ~ .hintbar .z1{display:none}
.zt:not(:checked) ~ .hintbar .z2{display:none}
.figcap{text-align:center;font-size:12.8px;color:var(--muted);margin:10px 0 30px}
@media (max-width:900px){
  .wrap{grid-template-columns:1fr;gap:0;padding:0 18px 64px}
  nav.toc{display:none}
  main{padding:28px 20px 36px;border-radius:12px}
  header.top{padding:34px 0 20px}
  header.top h1{font-size:25px}
  figure.fit{margin-left:0;margin-right:0}
  table{display:block;overflow-x:auto;white-space:nowrap}
}
"""

# 放大视图的视口尺寸按真实 viewBox 填回（见 _inline 里的 ZOOM_DIM）
CSS = (CSS.replace("__ZW__", f"{ZOOM_DIM['w']:.0f}")
          .replace("__ZH__", f"{ZOOM_DIM['h']:.0f}"))

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TITLE}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="eyebrow">{EYEBROW}</div>
    <h1>{TITLE}</h1>
    <p class="lead">{LEAD}</p>
  </header>
  <nav class="toc"><div class="toc-title">目录</div>{toc}</nav>
  <main>{body}</main>
</div>
</body>
</html>
"""

DST.write_text(HTML, encoding="utf-8")
print("written:", DST, len(HTML), "bytes")
