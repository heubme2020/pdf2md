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
CONVERTER_VERSION = 13  # 2026-10-07：无框线资产负债表「行解析」+ 框线表 + A1 自洽 98%（日台 2%→~90%）
