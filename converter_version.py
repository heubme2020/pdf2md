# -*- coding: utf-8 -*-
"""统一转换器版本 —— 四个开放交易所（A股/韩/日/台）共用一个版本号。

之前每个 crawler 各有一个 PARSER_VERSION（A股=10 / 韩=2 / 日=3 / 台=4），
**跨市场比大小毫无意义**：push.py 的版本护栏为了对齐，不得不维护一份
`_VERSION_MODULE` 映射「交易所 → 对应爬虫模块」，踩过坑（见 push.py 文件头注释）。

2026-09-30 起统一成一个 `CONVERTER_VERSION`：
  - 改任何转换器（pdf_to_md / edinet_html / dart_xml / unified_convert）就 bump 一次；
  - 四个市场一起「需重转」，一起按这个版本推送；
  - push.py 的版本护栏、crawler 的重转判据（doc_needs_reprocess）都读这一个数。

⚠️ 版本号必须是**单调递增**的整数。改算法时 bump，别复用旧值 —— 否则
「版本 == 线上」会让重转判据失效（doc_needs_reprocess 只认 !=）。
"""
CONVERTER_VERSION = 20  # v20 SEC Donnelley 章节标题；v19 英国 ESEF 两条修复(见下)
# v20：SEC Donnelley 10-K/10-Q 正文章节标题是 `<p id="item_X">`(不是 h1-h6 / div id / a id)，
#   _TextWalker 不认 → 正文零 ## 标题，get_section 把整份正文堆到最后一个 Item(伯克希尔 10-K 全堆进 Item 15)。
#   改为：p/li 等块元素 id 命中目录锚点时当 h2 处理，标题取正文原文(完整 "Item 1. Business Description")。重转美股 SEC。
# v19（2026-10-29 英国 ESEF 两个修复并在一起，英国 v19 重转还没跑完，别单独 bump）：
#   ① pdf2htmlEX 老格式(如 AstraZeneca 2021)每行文本被空 <div class="_ _N"> 定位标记 + 内联包装打散, 旧 regex (.*?)</div> 在嵌套 div 截断 → 只抽到行首几个字母(年报从 1300 万字塌成 26 万字)。改用控制字符边界占位符完整抽行。
#   ② pdf2htmlEX「自包含」格式的 <img> 内联 base64 SVG(data:image/svg+xml;base64) 没被剥, 一张 30-80 万字符、一份年报几十张 → 1M 撑到 ~10M(AstraZeneca 2025=232张源图/9.7M)。在 _uk_esef_to_md 里剥 <img> 标签。重转英国/欧洲 ESEF
