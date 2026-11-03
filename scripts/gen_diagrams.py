# SPDX-License-Identifier: Apache-2.0
"""生成项目文档用的三张图：拓扑图 / 结构图 / 流程图。

为什么不用 Mermaid：它依赖查看器支持（GitHub 行，很多本地预览不行）。
SVG 是图片，任何 Markdown 渲染器都能显示，而且**图是代码生成的**，改了
模块或流程就重跑这个脚本，不会出现"图比代码旧"的情况。

  python scripts/gen_diagrams.py          # 写到 docs/
  python scripts/gen_diagrams.py --check  # 只校验，不覆盖

约束：
- 不引入第三方依赖，只用标准库，保证任何环境都能重跑。
- 中文按两格宽估算排布，宁可留白也不让文字压到框外。
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs"

FONT = "ui-sans-serif, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif"
MONO = "ui-monospace, 'SFMono-Regular', Consolas, 'Liberation Mono', monospace"

# 配色：浅底深字，圆角卡片。深色主题下是一块白卡片，仍然读得清。
INK = "#1f2933"
SUB = "#52606d"
LINE = "#9aa5b1"
WHITE = "#ffffff"
PAGE = "#f7f9fb"

C_GATE = ("#e8f0fe", "#4a6fa5")  # 网关自身
C_LOCAL = ("#e6f4ea", "#2f7d4f")  # 本地模型
C_CLOUD = ("#fdecea", "#a63d40")  # 云端模型
C_DISK = ("#f3eefc", "#6b4c9a")  # 磁盘
C_CLIENT = ("#fff4e5", "#a86b1f")  # 客户端
C_WARN = ("#fff8e1", "#8a6d1f")  # 判定节点


def width(text: str, size: float) -> float:
    """粗估文本宽度：CJK 全角按 1.0em，ASCII 按 0.55em。"""
    total = 0.0
    for ch in text:
        total += size if ord(ch) > 0x2E80 else size * 0.55
    return total


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class Canvas:
    def __init__(self, w: int, h: int, title: str):
        self.w, self.h = w, h
        self.title = title
        self.parts: list[str] = []
        self.footer: list[str] = []
        self.problems: list[str] = []
        self.boxes: list[tuple[float, float, float, float, str]] = []
        self.segments: list[tuple[float, float, float, float]] = []

    def _fit(self, text: str, size: float, limit: float, where: str) -> None:
        """文字塞不进容器就记一笔。宁可报错，也不要出现被裁掉的半行字。"""
        need = width(text, size)
        if need > limit:
            self.problems.append(f"{where}: 「{text}」需 {need:.0f}px，只有 {limit:.0f}px")

    # ---- 基本元素 ----

    def box(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        title: str,
        lines: list[str] | None = None,
        color: tuple[str, str] = C_GATE,
        size: float = 15,
        small: float = 12.5,
        dashed: bool = False,
        rx: float = 10,
    ) -> None:
        fill, stroke = color
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        if x < 0 or y < 0 or x + w > self.w or y + h > self.h:
            self.problems.append(f"越界: 「{title}」在 ({x},{y},{w},{h})，画布 {self.w}x{self.h}")
        self.boxes.append((x, y, w, h, title))
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.4"{dash}/>'
        )
        lines = lines or []
        pad = 18
        self._fit(title, size, w - pad, f"标题「{title}」")
        for line in lines:
            self._fit(line, small, w - pad, f"正文「{line}」")
        block = size + (len(lines) * (small + 4))
        ty = y + (h - block) / 2 + size * 0.82
        self.parts.append(
            f'<text x="{x + w / 2}" y="{ty:.1f}" text-anchor="middle" font-size="{size}" '
            f'font-weight="600" fill="{INK}">{esc(title)}</text>'
        )
        for i, line in enumerate(lines):
            ly = ty + (i + 1) * (small + 4)
            self.parts.append(
                f'<text x="{x + w / 2}" y="{ly:.1f}" text-anchor="middle" font-size="{small}" '
                f'fill="{SUB}">{esc(line)}</text>'
            )

    def diamond(
        self,
        cx: float,
        cy: float,
        w: float,
        h: float,
        label: str,
        color: tuple[str, str] = C_WARN,
    ) -> None:
        fill, stroke = color
        pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
        self.parts.append(f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
        self.parts.append(
            f'<text x="{cx}" y="{cy + 4.5}" text-anchor="middle" font-size="12.5" '
            f'fill="{INK}">{esc(label)}</text>'
        )

    def arrow(
        self,
        pts: list[tuple[float, float]],
        label: str = "",
        dashed: bool = False,
        color: str = LINE,
        label_at: tuple[float, float] | None = None,
    ) -> None:
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        for i in range(len(pts) - 1):
            self.segments.append((pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]))
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5"{dash} '
            f'marker-end="url(#arrow)"/>'
        )
        if label:
            if label_at is None:
                mx = (pts[0][0] + pts[-1][0]) / 2
                my = (pts[0][1] + pts[-1][1]) / 2
            else:
                mx, my = label_at
            tw = width(label, 11.5) + 10
            self.parts.append(
                f'<rect x="{mx - tw / 2:.1f}" y="{my - 9:.1f}" width="{tw:.1f}" height="17" '
                f'rx="4" fill="{WHITE}" opacity="0.94"/>'
            )
            self.parts.append(
                f'<text x="{mx:.1f}" y="{my + 4:.1f}" text-anchor="middle" font-size="11.5" '
                f'fill="{SUB}">{esc(label)}</text>'
            )

    def caption(self, x: float, y: float, text: str, size: float = 12, anchor: str = "middle") -> None:
        self.footer.append(
            f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" fill="{SUB}">{esc(text)}</text>'
        )

    def note(self, x: float, y: float, lines: list[str], size: float = 12) -> None:
        for i, line in enumerate(lines):
            self.footer.append(
                f'<text x="{x}" y="{y + i * (size + 5)}" font-size="{size}" fill="{SUB}">{esc(line)}</text>'
            )

    # ---- 输出 ----

    def _check_crossings(self) -> None:
        """箭头不能从方框内部穿过去。

        端点贴着边框是正常的（进出框），所以边界 6px 以内不算。真正的问题是
        一条线横穿某个框的中央 —— 那会把框里的字割开。这种毛病肉眼审一遍
        三张图容易漏，写成检查就一次到位。
        """
        for x1, y1, x2, y2 in self.segments:
            steps = max(2, int(max(abs(x2 - x1), abs(y2 - y1)) / 4))
            for i in range(steps + 1):
                t = i / steps
                px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
                for bx, by, bw, bh, title in self.boxes:
                    inside = (
                        bx + 6 < px < bx + bw - 6
                        and by + 6 < py < by + bh - 6
                    )
                    if inside:
                        self.problems.append(
                            f"箭头穿过「{title}」内部：({px:.0f},{py:.0f})"
                        )
                        break
                else:
                    continue
                break

    def ascii_preview(self) -> str:
        """把几何降采样成字符网格，用来在没有图像库的环境里审版面。

        只画方框边框、标题首字和箭头轨迹；不画正文。够看出"哪里空了、
        哪里挤了、线绕得对不对"。
        """
        cw, ch = 10, 22  # 一个字符格代表的像素
        cols, rows = self.w // cw + 1, self.h // ch + 1
        grid = [[" "] * cols for _ in range(rows)]

        for idx, (x, y, w, h, title) in enumerate(self.boxes):
            c0, c1 = int(x / cw), int((x + w) / cw)
            r0, r1 = int(y / ch), int((y + h) / ch)
            for c in range(c0, c1 + 1):
                for r in range(r0, r1 + 1):
                    if r < 0 or r >= rows or c < 0 or c >= cols:
                        continue
                    edge = c in (c0, c1) or r in (r0, r1)
                    grid[r][c] = "+" if edge else "."
            mid_r, mid_c = (r0 + r1) // 2, (c0 + c1) // 2
            if idx < 10:
                label = str(idx)
            else:
                label = chr(ord("A") + idx - 10)
            if 0 <= mid_r < rows and 0 <= mid_c < cols:
                grid[mid_r][mid_c] = label

        for x1, y1, x2, y2 in self.segments:
            steps = max(2, int(max(abs(x2 - x1), abs(y2 - y1)) / 4))
            for i in range(steps + 1):
                t = i / steps
                c = int((x1 + (x2 - x1) * t) / cw)
                r = int((y1 + (y2 - y1) * t) / ch)
                if 0 <= r < rows and 0 <= c < cols and grid[r][c] in (" ", "."):
                    grid[r][c] = "."

        legend = "\n".join(f"  {i} = {b[4]}" for i, b in enumerate(self.boxes))
        return "   " + "-" * cols + "\n" + "\n".join("   " + "".join(row) for row in grid) + "\n" + legend

    def svg(self) -> str:
        self._check_crossings()
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{esc(self.title)}">'
            "<defs>"
            '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#9aa5b1"/></marker>'
            f"<style>text{{font-family:{FONT};}}</style>"
            "</defs>"
            f'<rect width="{self.w}" height="{self.h}" fill="{PAGE}"/>'
        )
        # 每个元素一行：生成物也是要被人打开看的，压成一行没法读、没法 diff
        return head + "\n" + "\n".join(self.parts + self.footer) + "\n</svg>\n"


# ==================================================================
# 图一：部署拓扑
# ==================================================================


def topology() -> Canvas:
    c = Canvas(980, 560, "steward 部署拓扑")

    c.box(30, 40, 250, 96, "智能体 / IDE", ["只用 OpenAI 兼容协议", "只知道一个 base_url"], C_CLIENT)
    c.box(370, 26, 250, 124, "steward 网关", [
        "127.0.0.1:27418/v1",
        "单进程 · 无状态",
        "鉴权 / 配额 / 裁剪",
    ], C_GATE)

    c.box(720, 26, 230, 96, "进阶模型（云端）", ["1M+ 上下文", "按次计费 · 受四道闸限制"], C_CLOUD)
    c.box(720, 152, 230, 92, "基础模型（本地）", ["64K–256K · 不固定", "llama.cpp / vLLM / Ollama"], C_LOCAL)
    c.box(720, 274, 230, 92, "磁盘", ["artifacts/ 交付物", "state/ 配额·系数"], C_DISK)
    c.box(720, 396, 230, 92, "本地文件系统", ["refs 白名单内", "默认关闭 · 有审计"], C_DISK, dashed=True)

    # 客户端 → 网关
    c.arrow([(280, 88), (370, 88)], "HTTP · 一把 st- key")
    # 网关 → 三个后端
    c.arrow([(620, 74), (720, 74)], "consult_cloud")
    c.arrow([(620, 124), (660, 124), (660, 198), (720, 198)], "日常请求")
    c.arrow([(620, 140), (660, 140), (660, 320), (720, 320)], "落盘 / 记账")
    c.arrow([(620, 150), (640, 150), (640, 442), (720, 442)], "refs（默认关）", dashed=True)

    c.note(30, 200, [
        "读图要点：",
        "· 智能体只看得到左边一个端点，不知道后面有几个模型。",
        "· 走哪条路由由网关内部决定：默认本地，只有 consult_cloud 才出去。",
        "· 云端拿到的只有委托单 + refs 展开的文件，看不到完整对话。",
    ], 12.5)

    c.caption(490, 528, "图 1　部署拓扑：一个端点，两个模型槽位，三处本地状态")
    return c


# ==================================================================
# 图二：模块结构
# ==================================================================


def structure() -> Canvas:
    c = Canvas(980, 690, "steward 模块结构")

    # 入口层
    c.box(380, 24, 220, 66, "server.py", ["FastAPI 路由 · 工具循环 · 鉴权"], C_GATE)
    c.box(660, 24, 200, 66, "__main__.py", ["serve / init / test / key", "ctx / refs / artifacts"], C_GATE, small=11.5)

    # 决策层
    c.box(30, 140, 200, 74, "guard.py", ["四道闸 + 去重", "配额持久化"], C_WARN)
    c.box(250, 140, 200, 74, "prompts.py", ["工具描述 · 委托质量闸", "骨架/交付物模板"], C_WARN)
    c.box(470, 140, 200, 74, "keys.py", ["项目 key 签发", "只存 hash"], C_WARN)
    c.box(690, 140, 200, 74, "refs.py", ["白名单 · 黑名单", "行区间 · 审计"], C_WARN, dashed=True)

    # 上下文层
    c.box(30, 274, 200, 92, "context.py", ["窗口探测 Prober", "精确计数 Measurer", "分级裁剪 Trimmer"], C_LOCAL)
    c.box(250, 274, 200, 92, "spine.py", ["任务骨架", "六行 · 缓存 · 兜底"], C_LOCAL)
    c.box(470, 274, 200, 92, "artifacts.py", ["落盘 · 抬头", "批量 · 回收"], C_LOCAL)

    # 传输层
    c.box(30, 420, 260, 88, "upstream.py", ["基础模型客户端", "404 自愈 · 探测"], C_CLOUD)
    c.box(320, 420, 260, 88, "cloud.py", ["进阶模型客户端", "超时 · 交付物外置"], C_CLOUD)
    c.box(610, 420, 280, 88, "responses.py", ["Responses ↔ Chat", "协议互转"], C_CLOUD)

    # 配置
    c.box(380, 542, 220, 56, "config.py", ["dataclass 解析 + validate"], C_DISK, small=11.5)

    # 关系：入口 → 决策层
    c.arrow([(470, 90), (470, 116), (130, 116), (130, 140)])
    c.arrow([(500, 90), (500, 140)])
    c.arrow([(560, 90), (570, 116), (570, 140)])
    c.arrow([(620, 90), (650, 116), (790, 116), (790, 140)])

    # 决策层 → 上下文层
    c.arrow([(130, 214), (130, 274)])
    c.arrow([(350, 214), (350, 274)])
    c.arrow([(570, 214), (570, 274)])
    c.arrow([(790, 214), (790, 250), (600, 250), (600, 274)], dashed=True)

    # 上下文层 → 传输层
    c.arrow([(130, 366), (130, 420)])
    c.arrow([(450, 366), (450, 420)])
    c.arrow([(620, 366), (700, 396), (700, 420)])

    c.note(30, 630, [
        "实线 = 直接调用；虚线 = 仅在开启时参与。config.py 被所有模块读取，为图面清晰未画。",
    ], 11.5)

    c.caption(490, 668, "图 2　模块结构：决策层 / 上下文层 / 传输层，全部在一个进程内")
    return c


# ==================================================================
# 图三：请求流程
# ==================================================================


def flow() -> Canvas:
    c = Canvas(1200, 930, "steward 请求流程")

    # ---- 主链路（中列） ----
    mx, mw = 420, 360
    cx = mx + mw / 2  # 600

    c.box(mx, 30, mw, 56, "客户端 POST /v1/chat/completions", ["带 st- 项目 key"], C_CLIENT)
    c.box(mx, 110, mw, 56, "鉴权 + 合并工具", ["注入 consult_cloud，保留客户端自带 tools"], C_GATE)
    c.box(mx, 190, mw, 56, "注入规则 + 配额提示", ["system 里写清「今天还剩几次」"], C_GATE)
    c.box(mx, 270, mw, 56, "每轮：精确计数", ["/tokenize 取真实 token，顺带校准系数"], C_GATE)
    c.diamond(cx, 388, 300, 76, "超出输入预算？")
    c.box(mx, 460, mw, 56, "发给基础模型", ["stream=false，网关收齐再回"], C_LOCAL)
    c.diamond(cx, 582, 320, 84, "模型调了 consult_cloud？")
    c.box(mx, 660, mw, 56, "返回客户端", ["附 X-Steward-* 响应头"], C_CLIENT)

    for y in (86, 166, 246, 326):
        c.arrow([(cx, y), (cx, y + 24)])
    c.arrow([(cx, 426), (cx, 460)], "否", label_at=(cx + 26, 444))
    c.arrow([(cx, 516), (cx, 540)])
    c.arrow([(cx, 624), (cx, 660)], "否", label_at=(cx + 26, 644))

    # ---- 左支线：裁剪 ----
    c.arrow([(cx - 150, 388), (320, 388)], "是", label_at=(420, 380))
    c.box(40, 336, 280, 116, "分级裁剪", [
        "1 思维链　2 图片",
        "3 工具摘要　4 丢工具对",
        "5 硬截断　6 结构修复",
        "丢了大量历史 → 生成骨架",
    ], C_LOCAL, size=14, small=11.5)
    c.arrow([(180, 452), (180, 488), (mx, 488)])

    # ---- 右支线：咨询 ----
    gx = 860
    c.arrow([(cx + 160, 582), (gx, 582)], "是", label_at=(800, 574))
    c.box(gx, 538, 300, 88, "质量闸 + 配额闸", ["不通过 → 回灌说明，不计数"], C_WARN, size=14, small=11.5)
    c.arrow([(gx + 150, 626), (gx + 150, 666)], "通过", label_at=(gx + 186, 646))
    c.box(gx, 666, 300, 96, "调进阶模型", ["展开 refs · 落盘全文 · 摘要回灌"], C_CLOUD, size=14, small=11.5)
    c.arrow([(gx + 150, 762), (gx + 150, 796)])
    c.box(gx, 796, 300, 66, "回灌 role=tool", ["下一轮从这里继续"], C_GATE, size=14, small=11.5)

    # 拒绝：绕过「调进阶模型」，直接汇入回灌
    c.arrow([(gx + 300, 560), (1178, 560), (1178, 829), (gx + 300, 829)], "拒绝", dashed=True, label_at=(1178, 700))

    # 回环：走主列与支线之间的空档，不压任何方框
    c.arrow([(gx, 829), (816, 829), (816, 298), (mx + mw, 298)], "下一轮", dashed=True, label_at=(816, 560))

    c.note(30, 856, [
        "读图要点：虚线是不花钱的路径（裁剪、退回、被闸拦下）；裁剪在循环内部，每一轮都判一次。",
    ], 11.5)

    c.caption(600, 906, "图 3　请求流程：本地的循环，云端只是循环里的一个可选步骤")
    return c


# ==================================================================


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验能否生成，不写文件")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for name, builder in (("topology", topology), ("structure", structure), ("flow", flow)):
        canvas = builder()
        if canvas.problems:
            print(f"\n{name}.svg 布局有问题：", file=sys.stderr)
            for p in canvas.problems:
                print(f"  · {p}", file=sys.stderr)
            return 1
        svg = canvas.svg()
        try:
            ET.fromstring(svg)  # 结构合法性：标签必须闭合
        except ET.ParseError as exc:
            print(f"{name}.svg 不是合法 XML：{exc}", file=sys.stderr)
            return 1
        if args.check:
            print(f"{name}.svg OK ({len(svg)} bytes)")
            continue
        path = OUT / f"{name}.svg"
        path.write_text(svg, encoding="utf-8")
        made.append(path)
        print(f"写入 {path.relative_to(ROOT)}  ({len(svg) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
