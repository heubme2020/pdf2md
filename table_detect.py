# -*- coding: utf-8 -*-
"""
三线表识别快速原型：检测横线(细长矩形) -> 聚类行/列 -> 文本归位 -> 输出 markdown 表格

用法:
    python table_detect.py <pdf路径>
"""
import sys
import fitz


def detect_hlines(page):
    """检测表格横线：细长水平矩形(re) 或 水平线段(l), 返回 [(y, x0, x1)]

    上交所部分年报表格边框用 l(line) 原语绘制(而非 re 矩形), 只看 re 会漏掉整张表。
    """
    hlines = []
    for d in page.get_drawings():
        width = d.get("width") or 0  # 部分 drawing 的 width 为 None
        for item in d["items"]:
            if item[0] == "re":
                r = item[1]
                if r.height < 1.5 and r.width > 50:
                    hlines.append((r.y0, r.x0, r.x1))
            elif item[0] == "l":
                p1, p2 = item[1], item[2]
                y0, y1 = min(p1.y, p2.y), max(p1.y, p2.y)
                x0, x1 = min(p1.x, p2.x), max(p1.x, p2.x)
                if y1 - y0 < 1.0 and x1 - x0 > 50 and width < 1.5:
                    hlines.append((y0, x0, x1))
    hlines.sort()
    return hlines


def detect_vlines(page):
    """检测表格竖线：垂直线段(l) 或 细长垂直矩形(re), 返回 [(x, y0, y1)]"""
    vlines = []
    for d in page.get_drawings():
        width = d.get("width") or 0  # 部分 drawing 的 width 为 None
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                x0, x1 = min(p1.x, p2.x), max(p1.x, p2.x)
                y0, y1 = min(p1.y, p2.y), max(p1.y, p2.y)
                if x1 - x0 < 1.0 and y1 - y0 > 10 and width < 1.5:
                    vlines.append((x0, y0, y1))
            elif item[0] == "re":
                r = item[1]
                if r.width < 1.5 and r.height > 10:
                    vlines.append((r.x0, r.y0, r.y1))
    vlines.sort()
    return vlines


def cluster_rows(hlines, y_tol=3.0):
    """按 y 聚类横线 -> 行分隔线列表, 每行 = (y, [(x0,x1),...])"""
    rows = []
    for y, x0, x1 in hlines:
        if rows and abs(y - rows[-1][0]) < y_tol:
            rows[-1][1].append((x0, x1))
        else:
            rows.append((y, [(x0, x1)]))
    return rows


def cluster_xs(xs, x_tol=3.0):
    """对 x 坐标列表聚类去重(排序后相邻 < tol 合并) -> 列边界列表"""
    xs = sorted(xs)
    cols = []
    for x in xs:
        if cols and abs(x - cols[-1]) < x_tol:
            continue
        cols.append(x)
    return cols


def table_blocks(rows):
    """把相邻行分隔线聚成表格块：连续 >= 2 条行线且间距合理(< 80pt)"""
    blocks = []
    cur = [rows[0]]
    for r in rows[1:]:
        if r[0] - cur[-1][0] < 80:
            cur.append(r)
        else:
            if len(cur) >= 2:
                blocks.append(cur)
            cur = [r]
    if len(cur) >= 2:
        blocks.append(cur)
    return blocks


# 列边界怎么找 —— **可切换，供版本对照**（见 pdf2md_referee.py 的变体机制）
#   "hlines" 原行为：优先竖线；没有竖线就退回横线端点并集
#   "words"  按词的横向空隙找列边界
COLUMN_STRATEGY = "hlines"

# **v8 兼容开关**（2026-09-29 加）：`True` = 关掉 v10 的两个修复，回到 v8 的行为。
# 用途：A/B 对照 —— 同一份文档跑两遍，量「v10 到底提升了多少」。
# 要复现的正是这两处：
#   ① 列边界扩展到覆盖块内全部文字
#   ② 落列外的词归最近列（v8 是直接丢弃）
# 因为 v9 ≡ v8 逐字节相同，所以"关掉这两个修复"就等于 v8。
LEGACY_V8 = False

# 诊断计数器（2026-09-29 加）：量「v10 的修复到底有没有机会生效」。
# 只有走**回退分支**（表格块里没有竖线）的表，才会碰到被修的那两处；
# 有竖线的表走 `cluster_xs(vxs)`，根本到不了那里。
# 用来回答"为什么同一个修复在 A股 上一点效果都没有"。
STATS = {"blocks": 0, "no_vlines": 0, "would_drop_v8": 0}


def columns_from_words(in_block, gap_ratio=1.8):
    """**按词的横向空隙找列边界** —— 处理"没有竖线的表"的正解。

    原行为（横线端点并集）只覆盖**数值区**，左边那列标签整体落在边界外 ——
    实测把「負債合計」整列丢掉。横线的端点是表格的**左右边缘**，它天生给不出内部列的分界。

    做法：把所有词的 [x0, x1] 投影到横轴上，合并重叠区间，
    **相邻区间之间的空隙中点**就是列边界 —— 这正是"列"在视觉上的定义。

    ⚠️ `gap_ratio` 是"多大才算列间缝"的门槛，按**词高**的倍数取：
    同一个单元格内部的词距通常远小于列间缝。取小了会把「当 連結 会計 年度」
    这种词距误判成列边界，取大了会把真的列粘起来。**1.8 是起点，要拿对照数据调。**
    """
    if not in_block:
        return None
    height = sorted((w[3] - w[1]) for w in in_block)
    unit = height[len(height) // 2] or 10.0
    min_gap = unit * gap_ratio

    spans = []
    for a, b in sorted((w[0], w[2]) for w in in_block):
        if spans and a - spans[-1][1] <= min_gap:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    if len(spans) < 2:
        return None
    cut = [spans[0][0]]
    for i in range(len(spans) - 1):
        cut.append((spans[i][1] + spans[i + 1][0]) / 2)
    cut.append(spans[-1][1])
    return cut


def _row_boundaries(in_blk, rows_y):
    """行边界 = 横线 y + 词 y 空隙中点（只在空隙附近没有横线时才补）。

    三线表（日股损益表）连续科目之间**没有横线**（只有小计前有线），
    线条法按横线分行会把「売上高」「売上原価」胶合成一个 cell（实测 S100RVWF：
    売上高 y中心122 / 売上原価 y中心135，中间无线，被粘成「売上高売上原価」）。
    这里把词 y 的空隙也当行界：相邻词中心差 > 词高 0.9 倍 → 中间补一条界。
    但若空隙中点附近已有横线（网格表每行有线），就不补，避免和线打架产生空行。
    """
    if len(in_blk) < 2:
        return sorted(rows_y)
    centers = sorted(set((w[1] + w[3]) / 2 for w in in_blk))
    heights = sorted(w[3] - w[1] for w in in_blk)
    unit = heights[len(heights) // 2] or 10.0
    bounds = list(rows_y)
    for i in range(len(centers) - 1):
        gap = centers[i + 1] - centers[i]
        if gap <= unit * 0.9:
            continue
        mid = (centers[i] + centers[i + 1]) / 2
        if any(abs(mid - ry) <= unit * 0.5 for ry in rows_y):
            continue  # 附近已有横线，别补
        bounds.append(mid)
    return sorted(bounds)


def extract_table(page, block):
    """从一个表格块(行分隔线列表)提取文本为二维数组"""
    top = block[0][0]
    bottom = block[-1][0]
    # 表格水平范围(用于过滤无关竖线, 如页面边线)
    left = min(x0 for _, segs in block for x0, _ in segs)
    right = max(x1 for _, segs in block for _, x1 in segs)
    # 列边界: 优先用表格区域内的竖线 x; 没有竖线时退回横线段端点并集
    vxs = [
        x for x, y0, y1 in detect_vlines(page)
        if y1 >= top - 2 and y0 <= bottom + 2 and left - 2 <= x <= right + 2
    ]
    words_all = page.get_text("words")
    in_blk = [w for w in words_all if top - 2 <= (w[1] + w[3]) / 2 <= bottom + 2]

    # ⚠️ **`words` 只在"没有竖线"的回退路径上用**。
    # 第一版让它优先于竖线，结果整体更差（逐份 +1/-18）：有竖线的表本来就分得对，
    # 换成按词距算反而被拆错。**问题从来只在"没有竖线"那条回退路径上**
    # （那里退回"横线端点并集"，只覆盖数值区，左边标签列整列落在边界外）。
    STATS["blocks"] += 1
    if len(vxs) < 2:
        STATS["no_vlines"] += 1        # 只有这种块才可能碰到 v10 修的那两处

    cols = None
    if len(vxs) >= 2:
        cols = cluster_xs(vxs)
    elif COLUMN_STRATEGY == "words" and len(in_blk) >= 2:
        cols = columns_from_words(in_blk)
    if cols is None:
        hxs = []
        for _, segs in block:
            for x0, x1 in segs:
                hxs.extend([x0, x1])
        cols = cluster_xs(hxs)
    # 行边界 = 行分隔线的 y + 词 y 空隙中点（三线表连续科目无横线，见 _row_boundaries）
    rows_y = _row_boundaries(in_blk, [r[0] for r in block])
    words = page.get_text("words")

    # ⚠️ **列边界必须覆盖块内的全部文字**（2026-09-28 修）。
    #    实测：没有竖线的表块（日股常见）退回"横线端点并集"当列边界，而那只覆盖**数值区** ——
    #    左边那列标签（`負債合計` 的 x0=50 而数值列从 59 开始）整体落在边界外，
    #    被下面的 `continue` **静默丢掉**，网格里只剩两列数字。
    #    更早还见过「資産合計 提取后变成 産合計」——同一个病根。
    #    **丢字是最不可接受的失败模式**：数字还在、科目名残缺，于是配对全部失效。
    # ⚠️ `cols` 可能是空的（列边界一条都没找出来）—— 直接写 `cols[0]` 会 IndexError。
    #    这是我加"列边界覆盖全部文字"时引入的回归，被 A股 评测当场抓到（688399）。
    if len(cols) < 2:
        return []
    if in_blk and not LEGACY_V8:
        cols[0] = min(cols[0], min(w[0] for w in in_blk))
        cols[-1] = max(cols[-1], max(w[2] for w in in_blk))

    # 初始化网格
    grid = [[[] for _ in range(len(cols) - 1)] for _ in range(len(rows_y) - 1)]
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        if not (top - 2 <= y0 <= bottom + 2):
            continue
        # 找行
        ri = None
        for i in range(len(rows_y) - 1):
            if rows_y[i] <= (y0 + y1) / 2 <= rows_y[i + 1]:
                ri = i
                break
        if ri is None:
            continue
        # 找列
        ci = None
        cx = (x0 + x1) / 2
        for j in range(len(cols) - 1):
            if cols[j] <= cx <= cols[j + 1]:
                ci = j
                break
        if ci is None:
            # 计数：v8 在这里会**丢掉这个词**。这个数字就是「修复能生效几次」。
            STATS["would_drop_v8"] += 1
            if LEGACY_V8:
                continue          # v8 行为：静默丢弃
            # ⚠️ **绝不丢字** —— 落在所有列之外的，归到最近的列。
            # 原来这里是 `continue`（静默丢弃），实测丢掉了整列标签的首字。
            ci = 0 if cx < cols[0] else len(cols) - 2
        grid[ri][ci].append(text)
    # 转成字符串网格
    result = []
    for row in grid:
        result.append(["".join(cell).strip() for cell in row])
    return result


def grid_to_md(grid):
    """二维网格 -> markdown 表格"""
    if not grid or not grid[0]:
        return ""
    ncol = len(grid[0])
    lines = []
    lines.append("| " + " | ".join(grid[0]) + " |")
    lines.append("| " + " | ".join(["---"] * ncol) + " |")
    for row in grid[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    pdf = sys.argv[1]
    doc = fitz.open(pdf)
    total_tables = 0
    for pno, page in enumerate(doc):
        hlines = detect_hlines(page)
        if len(hlines) < 4:
            continue
        rows = cluster_rows(hlines)
        blocks = table_blocks(rows)
        if not blocks:
            continue
        for bi, block in enumerate(blocks):
            grid = extract_table(page, block)
            if len(grid) < 2 or len(grid[0]) < 2:
                continue
            total_tables += 1
            print(f"\n===== 第 {pno+1} 页 表格 {bi+1} ({len(grid)} 行 x {len(grid[0])} 列) =====")
            print(grid_to_md(grid))
            if total_tables >= 5:
                print(f"\n... 已展示 {total_tables} 个表格, 停止")
                return
    print(f"\n共检测到 {total_tables} 个表格")


if __name__ == "__main__":
    main()
