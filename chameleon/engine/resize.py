"""
CHAMELEON resize engine  --  "move the furniture, don't stretch the photo".

Approach (pure heuristics, deterministic, offline, no model, no generated pixels)
-------------------------------------------------------------------------------
1. UNDERSTAND the design (the schema gives no semantic labels, so we infer them):
   * background  : any element covering ~the whole canvas
   * clusters    : elements that must travel together
                   - shared `group_id` (button = pill + label, badge, ribbon ...)
                   - a solid panel / image that fully contains other elements
   * decoration  : semi-transparent ungrouped shapes (bubbles, glows)
   * roles       : logo, product/hero image, headline, subhead, CTA, legal text, ...
                   -> each gets a priority (what must survive when space is tight)
2. RE-FLOW, never stretch. Several candidate layouts are built for the target
   canvas: a single stack, image|text splits, and 3-4 column bands
   (logo | product | copy | CTA). Inside each column blocks are stacked at ONE
   shared design scale `s` (the biggest that fits, found by binary search), text is
   re-wrapped with the real brand-font metrics and balanced, hero imagery soaks up
   leftover space, and rows that were side by side (badges) stay side by side
   when they fit.
3. SCORE every candidate (priority-weighted legibility/scale, hierarchy kept,
   balance, coverage) and keep the best.
4. If something cannot be made legible even at its floor size (e.g. a 190-char
   subhead in a 728x90 leaderboard) the lowest-priority block is dropped
   gracefully (collapsed to an invisible speck in the bottom margin). Ids are
   always preserved, nothing leaves the canvas, images keep their proportions.
5. Decoration is re-anchored to the nearest corner and shrunk until it no longer
   sits on top of text.

Never touched: element ids/count, font family, text content, asset pixels.
"""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# Text measurement (uses the very same fonts / skia metrics as the renderer)
# --------------------------------------------------------------------------


class _Fonts:
    def __init__(self) -> None:
        self.fm = None
        self.cw = None
        self.tried = False
        self.cache: dict = {}

    def _load(self) -> None:
        if self.tried:
            return
        self.tried = True
        try:
            from renderer.converter.brand_fonts import load_brand_font_manager
            from renderer.converter.wrap import char_widths

            here = Path(__file__).resolve().parent.parent
            for cand in (Path("brand_kit/fonts"), here / "brand_kit" / "fonts"):
                if cand.is_dir():
                    self.fm = load_brand_font_manager(str(cand))
                    self.cw = char_widths
                    break
        except Exception:  # pragma: no cover - fall back to heuristics
            self.fm = None

    def widths(self, family: str, bold: bool, italic: bool, text: str, size: float) -> list[float]:
        """Per-character advance widths at the exact pixel size (matches the renderer)."""
        key = (family, bold, italic, text, size)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        self._load()
        w: Optional[list[float]] = None
        if self.fm is not None:
            style = "bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular"
            try:
                w = [float(v) for v in self.cw(self.fm, family, "bold" if bold else "regular", style, size, text)]
                if len(w) != len(text):
                    w = None
            except Exception:
                w = None
        if w is None:  # crude fallback
            w = []
            for c in text:
                if c == " ":
                    k = 0.28
                elif c.isupper() or c.isdigit():
                    k = 0.66 if bold else 0.62
                elif c in "il.,;:'|!":
                    k = 0.30
                else:
                    k = 0.55 if bold else 0.52
                w.append(k * size)
        self.cache[key] = w
        return w


_FONTS = _Fonts()


def _wrap_lines(text: str, widths: list[float], maxw: float) -> list[float]:
    """Mirror of the renderer's greedy wrap; returns the pixel width of each line."""
    chars = list(text)
    lw = 0.0
    last = None
    for i, c in enumerate(chars):
        if c == "\n":
            lw, last = 0.0, None
            continue
        w = widths[i] if i < len(widths) else 0.0
        if lw + w > maxw and last is not None:
            chars[last] = "\n"
            lw = sum(widths[j] for j in range(last + 1, i + 1) if chars[j] != "\n")
            last = None
        else:
            lw += w
        if c == " ":
            last = i
    out, cur = [], 0.0
    for i, c in enumerate(chars):
        if c == "\n":
            out.append(cur)
            cur = 0.0
        else:
            cur += widths[i] if i < len(widths) else 0.0
    out.append(cur)
    return out


def _text_widths(el: dict, f: float) -> list[float]:
    bold = el.get("font_weight") == "bold" or any(
        (r.get("font_weight") == "bold") for r in (el.get("style_runs") or [])
    )
    italic = el.get("font_style") == "italic"
    return _FONTS.widths(el.get("font_family", "Solstice Sans"), bold, italic, str(el.get("content", "")), round(f * 4) / 4)


def _longest_word(text: str, widths: list[float]) -> float:
    best = cur = 0.0
    for i, c in enumerate(text):
        if c in " \n":
            best = max(best, cur)
            cur = 0.0
        else:
            cur += widths[i]
    return max(best, cur)


def _text_box(el: dict, f: float, avail_w: float, balance: bool = True) -> tuple[float, float, int]:
    """Return (box_width, box_height, n_lines) for text at font size f within avail_w."""
    text = str(el.get("content", ""))
    lh = float(el.get("line_height", 1.2) or 1.2)
    widths = _text_widths(el, f)
    lines = _wrap_lines(text, widths, avail_w)
    n = len(lines)
    mx = max(lines) if lines else 0.0
    w = min(avail_w, mx * 1.02 + 2.0)
    if balance and n > 1:
        lo, hi = max(_longest_word(text, widths), mx * 0.45), avail_w
        for _ in range(14):
            mid = (lo + hi) / 2
            if len(_wrap_lines(text, widths, mid)) <= n:
                hi = mid
            else:
                lo = mid
        cand = min(avail_w, hi * 1.01 + 2.0)
        if len(_wrap_lines(text, widths, cand)) <= n:
            w = cand
    return max(w, 1.0), n * f * lh, n


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------


def _rot_bbox(x: float, y: float, w: float, h: float, deg: float) -> tuple[float, float, float, float]:
    if not deg:
        return x, y, w, h
    r = math.radians(deg)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    bw, bh = w * c + h * s, w * s + h * c
    cx, cy = x + w / 2, y + h / 2
    return cx - bw / 2, cy - bh / 2, bw, bh


def _union(rects):
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


def _el_bbox(el: dict) -> tuple[float, float, float, float]:
    return _rot_bbox(el["x"], el["y"], el["width"], el["height"], el.get("rotation", 0) or 0)


def _inside(inner, outer, tol=2.0) -> bool:
    return (
        inner[0] >= outer[0] - tol
        and inner[1] >= outer[1] - tol
        and inner[0] + inner[2] <= outer[0] + outer[2] + tol
        and inner[1] + inner[3] <= outer[1] + outer[3] + tol
    )


def _overlap(a, b, pad=0.0) -> bool:
    return not (
        a[0] + a[2] + pad <= b[0] or b[0] + b[2] + pad <= a[0] or a[1] + a[3] + pad <= b[1] or b[1] + b[3] + pad <= a[1]
    )


def _alpha(color: str) -> float:
    m = re.match(r"rgba?\(([^)]*)\)", color or "")
    if not m:
        return 1.0
    p = [v.strip() for v in m.group(1).split(",")]
    try:
        return float(p[3]) if len(p) > 3 else 1.0
    except ValueError:
        return 1.0


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


class Ctx:
    def __init__(self, sw, sh, tw, th):
        self.sw, self.sh, self.tw, self.th = sw, sh, tw, th
        self.mn = min(tw, th)
        self.s_ideal = math.sqrt((tw * th) / (sw * sh))
        self.margin = max(8.0, 0.05 * self.mn)
        self.mingap = max(4.0, 0.016 * self.mn)
        self.gap0 = 24.0
        self.fmin = max(8.0, 0.011 * self.mn)
        self.role_fmin = {
            "headline": max(12.0, 0.025 * self.mn),
            "cta": max(11.0, 0.016 * self.mn),
            "sub": max(10.0, 0.014 * self.mn),
            "legal": max(7.0, 0.009 * self.mn),
        }
        self.img_min = {
            "logo": min(56.0, max(14.0, 0.28 * self.mn)),
            "product": min(140.0, max(24.0, 0.30 * self.mn)),
            "image": min(60.0, max(16.0, 0.12 * self.mn)),
        }
        self.cluster_min = min(40.0, max(12.0, 0.06 * self.mn))

    def gap(self, s: float) -> float:
        return max(self.mingap, self.gap0 * s)


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------


class Blk:
    prio = 50.0
    align = "center"
    row: Optional[int] = None
    role = "other"
    hero = False
    src_box = (0.0, 0.0, 1.0, 1.0)  # (x, y, w, h) in source space

    def measure(self, s: float, aw: float, ctx: Ctx):
        raise NotImplementedError

    def emit(self, pl: dict, x: float, y: float, out: dict) -> None:
        raise NotImplementedError

    def tiny(self, ctx: Ctx, x: float, y: float, slot_h: float, out: dict) -> float:
        raise NotImplementedError


class ImageBlk(Blk):
    def __init__(self, el: dict, role: str, missing: bool = False):
        self.el, self.role, self.missing = el, role, missing
        self.src_box = (el["x"], el["y"], el["width"], el["height"])
        self.hero = role == "product"

    def measure(self, s, aw, ctx):
        w0, h0 = self.el["width"], self.el["height"]
        ms = ctx.img_min.get(self.role, ctx.img_min["image"])
        sf = max(s, ms / max(1.0, min(w0, h0)))
        ok = True
        if w0 * sf > aw:
            sf = aw / w0
            ok = h0 * sf >= ms * 0.9 and w0 * sf >= ms * 0.9
        return w0 * sf, h0 * sf, ok, {"w": w0 * sf, "h": h0 * sf, "seff": sf}

    def emit(self, pl, x, y, out):
        out[self.el["id"]] = {"x": x, "y": y, "width": pl["w"], "height": pl["h"]}

    def tiny(self, ctx, x, y, slot_h, out):
        w0, h0 = self.el["width"], self.el["height"]
        w = 4.0
        h = w * h0 / max(w0, 1)
        out[self.el["id"]] = {"x": x, "y": y, "width": w, "height": h}
        return w


class TextBlk(Blk):
    def __init__(self, el: dict):
        self.el = el
        self.src_box = (el["x"], el["y"], el["width"], el["height"])
        a = el.get("text_align", "left")
        self.align = a if a in ("left", "right", "center") else "left"

    def measure(self, s, aw, ctx):
        f0 = float(self.el["font_size"])
        fmin = min(f0, ctx.role_fmin.get(self.role, ctx.fmin))
        f = max(round(f0 * s * 4) / 4, fmin)
        w, h, n = _text_box(self.el, f, aw)
        widths = _text_widths(self.el, f)
        ok = f >= fmin * 0.99 and _longest_word(str(self.el.get("content", "")), widths) <= aw + 0.5
        return w, h, ok, {"w": w, "h": h, "f": f, "seff": f / f0}

    def emit(self, pl, x, y, out):
        out[self.el["id"]] = {"x": x, "y": y, "width": pl["w"], "height": pl["h"], "font_size": pl["f"]}

    def tiny(self, ctx, x, y, slot_h, out):
        w = 60.0
        _, h, _ = _text_box(self.el, 1.0, w, balance=False)
        out[self.el["id"]] = {"x": x, "y": y, "width": w, "height": min(h, max(slot_h, 2.0)), "font_size": 1.0}
        return w


class ClusterBlk(Blk):
    """Elements that travel together; scaled uniformly (similarity transform)."""

    def __init__(self, members: list[dict]):
        self.members = members
        rects = [_el_bbox(m) for m in members]
        self.bx, self.by, self.bw, self.bh = _union(rects)
        self.src_box = (self.bx, self.by, self.bw, self.bh)
        self.cx, self.cy = self.bx + self.bw / 2, self.by + self.bh / 2
        self.rotated = any((m.get("rotation") or 0) != 0 for m in members)
        self.texts = [m for m in members if m["type"] == "text"]
        self.pill = None
        shp = [m for m in members if m["type"] == "shape"]
        if len(members) == 2 and len(shp) == 1 and len(self.texts) == 1 and not self.rotated:
            sh_, tx = shp[0], self.texts[0]
            if _inside(_el_bbox(tx), _el_bbox(sh_), 4.0):
                f0 = float(tx["font_size"])
                tw0 = _wrap_lines(str(tx.get("content", "")), _text_widths(tx, f0), 1e9)[0]
                lh = float(tx.get("line_height", 1.2) or 1.2)
                self.pill = {
                    "shape": sh_, "text": tx, "f0": f0, "lh": lh,
                    "px": min(1.6, max(0.5, (sh_["width"] - tw0) / 2 / f0)),
                    "py": min(0.9, max(0.25, (sh_["height"] - f0 * lh) / 2 / f0)),
                }

    def _floor(self, ctx):
        fl = ctx.cluster_min / max(1.0, min(self.bw, self.bh))
        base = ctx.role_fmin["cta"] if self.role == "cta" else ctx.fmin
        for t in self.texts:
            f0 = float(t["font_size"])
            fl = max(fl, min(f0, base) / f0)
        return fl

    def _measure_pill(self, s, aw, ctx):
        pl_ = self.pill
        tx, f0, lh = pl_["text"], pl_["f0"], pl_["lh"]
        fmin = min(f0, ctx.role_fmin["cta"] if self.role == "cta" else ctx.fmin)
        f = max(round(f0 * s * 4) / 4, fmin)
        while True:
            tw_ = _wrap_lines(str(tx.get("content", "")), _text_widths(tx, f), 1e9)[0]
            W = tw_ + 2 * pl_["px"] * f
            if W <= aw or f <= fmin:
                break
            f = max(fmin, f - 0.25)
        H = f * lh + 2 * pl_["py"] * f
        ok = W <= aw + 0.5
        return W, H, ok, {"w": W, "h": H, "f": f, "tw": tw_, "seff": f / f0, "pill": True}

    def measure(self, s, aw, ctx):
        if self.pill:
            return self._measure_pill(s, aw, ctx)
        fl = self._floor(ctx)
        sf = max(s, fl)
        ok = True
        if self.bw * sf > aw:
            sf = aw / self.bw
            ok = sf >= fl * 0.97
        return self.bw * sf, self.bh * sf, ok, {"w": self.bw * sf, "h": self.bh * sf, "sf": sf, "seff": sf}

    def emit(self, pl, x, y, out):
        if pl.get("pill"):
            P_ = self.pill
            sh_, tx = P_["shape"], P_["text"]
            W, H, f = pl["w"], pl["h"], pl["f"]
            d = {"x": x, "y": y, "width": W, "height": H}
            if sh_.get("corner_radius"):
                d["corner_radius"] = sh_["corner_radius"] / max(1.0, sh_["height"]) * H
            if sh_.get("stroke_width"):
                d["stroke_width"] = sh_["stroke_width"] * f / P_["f0"]
            out[sh_["id"]] = d
            th_ = f * P_["lh"]
            out[tx["id"]] = {"x": x + (W - min(W, pl["tw"] * 1.02 + 2)) / 2, "y": y + (H - th_) / 2,
                             "width": min(W, pl["tw"] * 1.02 + 2), "height": th_, "font_size": f}
            return
        sf = pl["sf"]
        ncx, ncy = x + pl["w"] / 2, y + pl["h"] / 2
        for m in self.members:
            mcx, mcy = m["x"] + m["width"] / 2, m["y"] + m["height"] / 2
            w, h = m["width"] * sf, m["height"] * sf
            cx, cy = ncx + (mcx - self.cx) * sf, ncy + (mcy - self.cy) * sf
            d = {"x": cx - w / 2, "y": cy - h / 2, "width": w, "height": h}
            if m["type"] == "text":
                f = max(1.0, round(float(m["font_size"]) * sf * 4) / 4)
                d["font_size"] = f
                n_src = len(_wrap_lines(str(m.get("content", "")), _text_widths(m, float(m["font_size"])), m["width"]))
                ww = w
                for _ in range(12):
                    if len(_wrap_lines(str(m.get("content", "")), _text_widths(m, f), ww)) <= n_src or ww > 1.3 * w:
                        break
                    ww *= 1.03
                if ww != w:
                    al = m.get("text_align", "left")
                    d["x"] = cx - ww / 2 if al == "center" else (cx + w / 2 - ww if al == "right" else cx - w / 2)
                    d["width"] = ww
            if m["type"] == "shape":
                if m.get("corner_radius"):
                    d["corner_radius"] = m["corner_radius"] * sf
                if m.get("stroke_width"):
                    d["stroke_width"] = m["stroke_width"] * sf
            out[m["id"]] = d

    def tiny(self, ctx, x, y, slot_h, out):
        self.pill = None
        sf = 4.0 / max(self.bw, 1.0)
        pl = {"w": self.bw * sf, "h": self.bh * sf, "sf": sf}
        self.emit(pl, x, y, out)
        for t in self.texts:
            out[t["id"]]["font_size"] = 1.0
        return pl["w"]


class RowBlk(Blk):
    def __init__(self, kids: list[Blk]):
        self.kids = kids
        self.prio = max(k.prio for k in kids)
        self.hero = False

    def measure(self, s, aw, ctx):
        gap = max(ctx.mingap, 0.6 * ctx.gap0 * s)
        lines: list[list] = []
        cur: list = []
        cw = 0.0
        ok_all = True
        for k in self.kids:
            w, h, ok, pl = k.measure(s, aw, ctx)
            ok_all &= ok
            if cur and cw + gap + w > aw:
                lines.append(cur)
                cur, cw = [], 0.0
            cur.append((k, w, h, pl))
            cw += (gap if len(cur) > 1 else 0) + w
        if cur:
            lines.append(cur)
        lw = [sum(i[1] for i in ln) + gap * (len(ln) - 1) for ln in lines]
        lh = [max(i[2] for i in ln) for ln in lines]
        W, H = max(lw), sum(lh) + gap * (len(lines) - 1)
        return W, H, ok_all, {"lines": lines, "lw": lw, "lh": lh, "gap": gap, "w": W, "h": H}

    def emit(self, pl, x, y, out):
        cy = y
        for ln, lw, lh in zip(pl["lines"], pl["lw"], pl["lh"]):
            cx = x + (pl["w"] - lw) / 2
            for k, w, h, kp in ln:
                k.emit(kp, cx, cy + (lh - h) / 2, out)
                cx += w + pl["gap"]
            cy += lh + pl["gap"]

    def leaves(self, pl):
        return [(k, kp) for ln in pl["lines"] for (k, w, h, kp) in ln]


# --------------------------------------------------------------------------
# Background luminance sampler (read-only; used to avoid text on low-contrast /
# busy regions). Never modifies or generates any pixels.
# --------------------------------------------------------------------------


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _rgb_lum(r: float, g: float, b: float) -> float:
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _parse_rgba(color: str) -> tuple[float, float, float, float]:
    m = re.match(r"rgba?\(([^)]*)\)", color or "")
    if not m:
        return 255.0, 255.0, 255.0, 1.0
    p = [v.strip() for v in m.group(1).split(",")]
    try:
        r, g, b = float(p[0]), float(p[1]), float(p[2])
        a = float(p[3]) if len(p) > 3 else 1.0
        return r, g, b, a
    except (ValueError, IndexError):
        return 255.0, 255.0, 255.0, 1.0


class BgInfo:
    """Luminance map of the (full-bleed) background in target-canvas coordinates."""

    def __init__(self, bg_els: list[dict], tw: float, th: float):
        self.tw, self.th = tw, th
        self.grid = None  # list of rows of luminance, or constant
        self.const: Optional[float] = None
        self.gw = self.gh = 0
        try:
            imgs = [e for e in bg_els if e["type"] == "image"]
            shapes = [e for e in bg_els if e["type"] == "shape"]
            if imgs:
                self._from_image(imgs[0])
            elif shapes:
                r, g, b, a = _parse_rgba(shapes[0].get("fill_color", ""))
                self.const = _rgb_lum(r, g, b)
        except Exception:
            self.grid = None
            self.const = None

    def _from_image(self, el: dict) -> None:
        from PIL import Image

        ad = _assets_dir()
        path = ad / str(el.get("asset_id", "")) if ad else None
        if not path or not path.is_file():
            return
        im = Image.open(path).convert("RGB")
        iw, ih = im.size
        sc = max(self.tw / iw, self.th / ih)  # 'cover'
        vw, vh = self.tw / sc, self.th / sc  # visible window in image px
        ox, oy = (iw - vw) / 2, (ih - vh) / 2
        im = im.crop((int(ox), int(oy), int(ox + vw + 0.999), int(oy + vh + 0.999)))
        gw = 96
        gh = max(1, int(round(gw * self.th / self.tw)))
        im = im.resize((gw, gh))
        px = list(im.getdata())
        self.gw, self.gh = gw, gh
        self.grid = [[_rgb_lum(*px[y * gw + x]) for x in range(gw)] for y in range(gh)]

    def samples(self, rect) -> list[float]:
        if self.const is not None:
            return [self.const]
        if self.grid is None:
            return []
        x, y, w, h = rect
        x0 = max(0, int(x / self.tw * self.gw))
        x1 = min(self.gw, max(x0 + 1, int((x + w) / self.tw * self.gw + 0.999)))
        y0 = max(0, int(y / self.th * self.gh))
        y1 = min(self.gh, max(y0 + 1, int((y + h) / self.th * self.gh + 0.999)))
        return [self.grid[j][i] for j in range(y0, y1) for i in range(x0, x1)]

    def contrast(self, rect, color: str, opacity: float, font_px: float) -> Optional[float]:
        """Return contrast margin in [0,1+]: >=1 means passes WCAG (3:1 large / 4.5:1 body)."""
        smp = self.samples(rect)
        if not smp:
            return None
        r, g, b, a = _parse_rgba(color)
        a *= opacity
        yt = _rgb_lum(r, g, b)
        worst = 1e9
        smp.sort()
        # judge the worst 10% of the pixels under the text
        pick = smp[: max(1, len(smp) // 10)] if yt > 0.5 else smp[-max(1, len(smp) // 10):]
        for yb in pick:
            ye = a * yt + (1 - a) * yb
            hi, lo = max(ye, yb), min(ye, yb)
            worst = min(worst, (hi + 0.05) / (lo + 0.05))
        need = 3.0 if font_px >= 24 else 4.5
        return worst / need


# --------------------------------------------------------------------------
# Parsing: understand the source design
# --------------------------------------------------------------------------

_CTA_RE = re.compile(
    r"\b(shop|buy|order|learn|get|join|sign|book|try|download|start|discover|explore|subscribe|register|apply|"
    r"call|visit|see|view|read|watch|play|now|more|claim|reserve)\b",
    re.I,
)


def _assets_dir() -> Optional[Path]:
    here = Path(__file__).resolve().parent.parent
    for cand in (Path("brand_kit/assets"), here / "brand_kit" / "assets"):
        if cand.is_dir():
            return cand
    return None


class Parsed:
    def __init__(self):
        self.bg: list[dict] = []
        self.deco: list[dict] = []
        self.blocks: list[Blk] = []
        self.variants: list = []


def _parse(source: dict, ctx: Ctx) -> Parsed:
    sw, sh = ctx.sw, ctx.sh
    els = [copy.deepcopy(e) for e in source["elements"]]
    for e in els:
        e["z_index"] = e.get("z_index", 0) or 0
        e["x"], e["y"] = float(e["x"]), float(e["y"])
        e["width"], e["height"] = float(e["width"]), float(e["height"])
    P = Parsed()
    assigned: set[str] = set()
    canvas_area = sw * sh

    # ---- background ------------------------------------------------------
    for e in els:
        if (
            e["type"] in ("image", "shape")
            and e["width"] * e["height"] >= 0.85 * canvas_area
            and (e.get("rotation") or 0) == 0
            and e["x"] <= 0.05 * sw
            and e["y"] <= 0.05 * sh
        ):
            P.bg.append(e)
            assigned.add(e["id"])

    # ---- group clusters --------------------------------------------------
    groups: dict[str, list[dict]] = {}
    for e in els:
        g = e.get("group_id")
        if g and e["id"] not in assigned:
            groups.setdefault(g, []).append(e)
    clusters: list[list[dict]] = []
    for g, mem in groups.items():
        clusters.append(sorted(mem, key=lambda m: m["z_index"]))
        assigned.update(m["id"] for m in mem)

    # ---- containment clusters (panel / card / image with overlay text) -----
    free = [e for e in els if e["id"] not in assigned]
    containers = []
    for e in free:
        area = e["width"] * e["height"]
        solid = _alpha(e.get("fill_color", "")) * float(e.get("opacity", 1) or 1) >= 0.5
        if e["type"] == "shape" and solid and area <= 0.7 * canvas_area and e.get("shape_type") != "line":
            containers.append(e)
        elif e["type"] == "image" and area <= 0.55 * canvas_area:
            containers.append(e)
    containers.sort(key=lambda e: -(e["width"] * e["height"]))
    for c in containers:
        if c["id"] in assigned:
            continue
        cb = _el_bbox(c)
        kids = [
            o
            for o in free
            if o["id"] not in assigned
            and o is not c
            and o["z_index"] > c["z_index"]
            and o["type"] != "image"
            and _inside(_el_bbox(o), cb)
            and (o["width"] * o["height"]) < 0.8 * (c["width"] * c["height"])
        ]
        if kids:
            clusters.append(sorted([c] + kids, key=lambda m: m["z_index"]))
            assigned.add(c["id"])
            assigned.update(k["id"] for k in kids)

    # ---- decoration (semi-transparent loose shapes) ------------------------
    for e in els:
        if e["id"] in assigned:
            continue
        faint = float(e.get("opacity", 1) or 1) * _alpha(e.get("fill_color", "")) < 0.7
        if e["type"] == "shape" and faint and e.get("shape_type") != "line":
            P.deco.append(e)
            assigned.add(e["id"])

    # ---- remaining singles / variants ----------------------------------------
    assets = _assets_dir()

    def single(e):
        if e["type"] == "text":
            return TextBlk(e)
        if e["type"] == "image":
            miss = bool(assets) and not (assets / str(e.get("asset_id", ""))).is_file()
            return ImageBlk(e, "image", missing=miss)
        return ClusterBlk([e])

    def is_panel(mem):
        return mem[0]["type"] == "shape" and sum(1 for m in mem if m["type"] == "text") >= 2

    def build(explode):
        blocks = [single(e) for e in els if e["id"] not in assigned]
        panels = {}
        for mem in clusters:
            if explode and is_panel(mem):
                panels[mem[0]["id"]] = [m["id"] for m in mem[1:]]
                blocks += [single(m) for m in mem[1:]]
            else:
                blocks.append(ClusterBlk(mem))
        _finish(blocks, ctx)
        return blocks, panels, ctx.gap0

    P.variants = [build(False)]
    if any(is_panel(m) for m in clusters):
        P.variants.append(build(True))
    P.blocks = P.variants[0][0]
    ctx.gap0 = P.variants[0][2]
    return P


def _finish(blocks: list, ctx: "Ctx") -> None:
    """Assign roles / priorities / reading order / rows to a list of blocks (in place)."""
    sw, sh = ctx.sw, ctx.sh
    canvas_area = sw * sh
    # ---- roles -------------------------------------------------------------
    imgs = [b for b in blocks if isinstance(b, ImageBlk)]
    for b in imgs:
        if b.missing:
            b.role = "missing"
            continue
        key = (str(b.el.get("id", "")) + " " + str(b.el.get("asset_id", ""))).lower()
        area = b.el["width"] * b.el["height"]
        asp = b.el["width"] / max(1.0, b.el["height"])
        cy = (b.el["y"] + b.el["height"] / 2) / sh
        if re.search(r"logo|brand|lockup|wordmark", key) or (
            asp >= 2.2 and area < 0.15 * canvas_area and (cy < 0.25 or cy > 0.85)
        ):
            b.role = "logo"
    rest = [b for b in imgs if b.role != "logo" and not b.missing]
    if rest:
        prod = max(rest, key=lambda b: b.el["width"] * b.el["height"])
        prod.role = "product"
        prod.hero = True

    texts = [b for b in blocks if isinstance(b, TextBlk)]
    fmax = max([float(b.el["font_size"]) for b in texts] or [1.0])
    head = max(texts, key=lambda b: (float(b.el["font_size"]), -b.el["y"]), default=None)

    for b in blocks:
        if isinstance(b, ImageBlk):
            b.prio = {"logo": 95.0, "product": 90.0}.get(b.role, 50.0)
            if b.missing:
                b.prio = 0.0
            b.align = "center"
        elif isinstance(b, TextBlk):
            f = float(b.el["font_size"])
            alpha = _alpha(b.el.get("color", "")) * float(b.el.get("opacity", 1) or 1)
            content = str(b.el.get("content", ""))
            if b is head:
                b.prio, b.role = 100.0, "headline"
            elif _CTA_RE.search(content) and len(content.split()) <= 6:
                b.prio, b.role = 88.0, "cta"
            elif f <= 0.4 * fmax and (alpha < 0.8 or f <= 14):
                b.prio, b.role = 20.0, "legal"
            else:
                b.prio, b.role = 55.0 + 15.0 * min(1.0, f / fmax), "sub"
        else:
            assert isinstance(b, ClusterBlk)
            txt = " ".join(str(t.get("content", "")) for t in b.texts)
            if b.texts and _CTA_RE.search(txt) and len(txt.split()) <= 6:
                b.prio, b.role = 88.0, "cta"
            elif not b.texts:
                b.prio, b.role = 12.0, "deco"
            elif b.rotated:
                b.prio, b.role = 30.0, "ribbon"
            else:
                b.prio, b.role = 40.0, "badge"
            cxm = b.cx / sw
            b.align = "center" if abs(cxm - 0.5) < 0.12 else ("left" if cxm < 0.5 else "right")

    # ---- reading order and rows ---------------------------------------------
    blocks.sort(key=lambda b: (b.src_box[1] + b.src_box[3] / 2, b.src_box[0]))
    rid = 0
    for i, b in enumerate(blocks):
        if isinstance(b, TextBlk) or (isinstance(b, ImageBlk) and b.role == "product"):
            continue
        for o in blocks[:i]:
            if isinstance(o, TextBlk) or (isinstance(o, ImageBlk) and o.role == "product"):
                continue
            a, c = b.src_box, o.src_box
            ov = min(a[1] + a[3], c[1] + c[3]) - max(a[1], c[1])
            disjoint = a[0] >= c[0] + c[2] - 1 or c[0] >= a[0] + a[2] - 1
            if ov >= 0.55 * min(a[3], c[3]) and disjoint:
                if o.row is None:
                    rid += 1
                    o.row = rid
                b.row = o.row
                break
    # typical source gap between consecutive blocks (design rhythm)
    gaps = []
    for a, b in zip(blocks, blocks[1:]):
        g = b.src_box[1] - (a.src_box[1] + a.src_box[3])
        if g > 0:
            gaps.append(g)
    if gaps:
        gaps.sort()
        ctx.gap0 = min(48.0, max(14.0, gaps[len(gaps) // 2]))


# --------------------------------------------------------------------------
# Column solver
# --------------------------------------------------------------------------


def _compose(active: list[Blk]) -> list[Blk]:
    items: list[Blk] = []
    i = 0
    while i < len(active):
        b = active[i]
        if b.row is not None:
            j = i
            kids = []
            while j < len(active) and active[j].row == b.row:
                kids.append(active[j])
                j += 1
            items.append(RowBlk(kids) if len(kids) > 1 else kids[0])
            i = j
        else:
            items.append(b)
            i += 1
    return items


def _measure_all(items, s, beta, aw, ctx):
    res = []
    ok = True
    for it in items:
        ss = s * beta if it.hero else s
        w, h, o, pl = it.measure(ss, aw, ctx)
        ok &= o
        res.append((it, w, h, pl))
    return res, ok


def _total_h(res, ctx, s):
    return sum(r[2] for r in res) + ctx.gap(s) * (len(res) - 1)


def _solve_column(blocks: list[Blk], rect, ctx: Ctx):
    x0, y0, W, H = rect
    active = list(blocks)
    dropped: list[Blk] = []
    items: list[Blk] = []
    while active:
        items = _compose(active)
        res, ok = _measure_all(items, 0.0, 1.0, W, ctx)
        if ok and _total_h(res, ctx, 0.0) <= H:
            break
        victim = min(active, key=lambda b: b.prio)
        active.remove(victim)
        dropped.append(victim)
    if not active:
        return {"out": {}, "leaves": [], "rects": [], "dropped": dropped, "s": 0.0}

    def fits(s, beta=1.0):
        r, o = _measure_all(items, s, beta, W, ctx)
        return o and _total_h(r, ctx, s) <= H

    lo, hi = 0.0, max(0.05, 1.3 * ctx.s_ideal)
    for _ in range(40):
        mid = (lo + hi) / 2
        if fits(mid):
            lo = mid
        else:
            hi = mid
    s = lo
    beta = 1.0
    if any(it.hero for it in items):
        lo_b, hi_b = 1.0, 1.8
        for _ in range(30):
            mid = (lo_b + hi_b) / 2
            if fits(s, mid):
                lo_b = mid
            else:
                hi_b = mid
        beta = lo_b
    res, _ = _measure_all(items, s, beta, W, ctx)
    gap = ctx.gap(s)
    extra = H - _total_h(res, ctx, s)
    add = min(extra / (len(res) - 1), gap * 1.2) if len(res) > 1 else 0.0
    gap2 = gap + max(0.0, add)
    total = sum(r[2] for r in res) + gap2 * (len(res) - 1)
    y = y0 + (H - total) / 2
    out: dict = {}
    leaves, rects = [], []
    for it, w, h, pl in res:
        al = it.align if not isinstance(it, RowBlk) else "center"
        if al == "left":
            x = x0
        elif al == "right":
            x = x0 + W - w
        else:
            x = x0 + (W - w) / 2
        it.emit(pl, x, y, out)
        if isinstance(it, RowBlk):
            leaves += it.leaves(pl)
        else:
            pl["rect"] = (x, y, w, h)
            leaves.append((it, pl))
        rects.append((x, y, w, h, it))
        y += h + gap2
    return {"out": out, "leaves": leaves, "rects": rects, "dropped": dropped, "s": s}


# --------------------------------------------------------------------------
# Candidate layouts
# --------------------------------------------------------------------------


def _build(assign: list[list[Blk]], weights: list[float], ctx: Ctx):
    m = ctx.margin
    X, Y, W, H = m, m, ctx.tw - 2 * m, ctx.th - 2 * m
    colgap = max(2 * ctx.mingap, 0.03 * ctx.tw) if len(assign) > 1 else 0.0
    avail = W - colgap * (len(assign) - 1)
    tot = sum(weights)
    x = X
    out: dict = {}
    leaves, rects, dropped = [], [], []
    for col, wt in zip(assign, weights):
        cw = avail * wt / tot
        r = _solve_column(col, (x, Y, cw, H), ctx)
        out.update(r["out"])
        leaves += r["leaves"]
        rects += r["rects"]
        dropped += r["dropped"]
        x += cw + colgap
    return {"out": out, "leaves": leaves, "rects": rects, "dropped": dropped, "ncols": len(assign)}


def _score(c, all_blocks: list[Blk], ctx: Ctx) -> float:
    flat = []
    for b in all_blocks:
        flat.append(b)
    tot = sum(b.prio for b in flat) or 1.0
    got = 0.0
    for b, pl in c["leaves"]:
        q = min(1.15, pl["seff"] / ctx.s_ideal) if ctx.s_ideal > 0 else 1.0
        if isinstance(b, ImageBlk) and b.role == "logo":  # a logo must stay visibly legible
            gm = math.sqrt(pl["w"] * pl["h"])
            q *= 0.5 + 0.5 * min(1.0, gm / (0.2 * math.sqrt(ctx.tw * ctx.th)))
        if isinstance(b, TextBlk) and getattr(ctx, "bginfo", None) is not None and pl.get("rect"):
            m = ctx.bginfo.contrast(pl["rect"], b.el.get("color", ""), float(b.el.get("opacity", 1) or 1), pl["f"])
            if m is not None and m < 1.0:
                q *= max(0.2, m)
        got += b.prio * max(0.0, q)
    sc = got / tot
    rects = [r[:4] for r in c["rects"]]
    if rects:
        area = sum(r[2] * r[3] for r in rects)
        sc += 0.15 * min(1.0, (area / (ctx.tw * ctx.th)) / 0.30)
        bx = _union(rects)
        dx = abs((bx[0] + bx[2] / 2) - ctx.tw / 2) / ctx.tw
        dy = abs((bx[1] + bx[3] / 2) - ctx.th / 2) / ctx.th
        sc -= 0.2 * (dx + dy)
    ncols = c.get("ncols", 1)
    if ncols > 1:
        closeness = max(0.0, 1.0 - abs(math.log((ctx.tw / ctx.th) / (ctx.sw / ctx.sh))) / math.log(2.2))
        sc -= 0.25 * closeness
    fonts = {id(b): pl.get("f") for b, pl in c["leaves"] if isinstance(b, TextBlk)}
    heads = [b for b in all_blocks if isinstance(b, TextBlk) and b.role == "headline"]
    if heads and id(heads[0]) in fonts:
        hf = fonts[id(heads[0])]
        if any(f > hf * 1.001 for k, f in fonts.items() if k != id(heads[0])):
            sc -= 0.1
    return sc


def _candidates(blocks: list[Blk]):
    blocks = [b for b in blocks if not (isinstance(b, ImageBlk) and b.missing)]
    imgs = [b for b in blocks if isinstance(b, ImageBlk)]
    others = [b for b in blocks if b not in imgs]
    cta = [b for b in blocks if b.role == "cta"]
    rest_no_cta = [b for b in others if b not in cta]
    logo = [b for b in blocks if b.role == "logo"]
    prod = [b for b in blocks if b.role == "product"]
    yield [blocks], [1.0]
    if imgs and others:
        for w in [(0.3, 0.7), (0.4, 0.6), (0.5, 0.5), (0.35, 0.65), (0.45, 0.55)]:
            yield [imgs, others], list(w)
            yield [others, imgs], [w[1], w[0]]
    if imgs and rest_no_cta and cta:
        for w in [(0.2, 0.55, 0.25), (0.25, 0.5, 0.25), (0.3, 0.4, 0.3), (0.15, 0.65, 0.2), (0.2, 0.6, 0.2), (0.25, 0.45, 0.3), (0.35, 0.4, 0.25), (0.3, 0.45, 0.25)]:
            yield [imgs, rest_no_cta, cta], list(w)
    if logo and rest_no_cta and cta:
        t = [b for b in rest_no_cta if b not in logo and b not in prod]
        for w in [(0.2, 0.55, 0.25), (0.25, 0.5, 0.25), (0.15, 0.65, 0.2), (0.2, 0.6, 0.2), (0.3, 0.45, 0.25), (0.35, 0.4, 0.25)]:
            yield [logo, t, cta], list(w)
    if prod and logo and rest_no_cta and cta:
        t = [b for b in rest_no_cta if b not in logo and b not in prod]
        for w in [(0.2, 0.2, 0.4, 0.2), (0.15, 0.2, 0.45, 0.2), (0.25, 0.2, 0.3, 0.25), (0.25, 0.15, 0.4, 0.2), (0.3, 0.15, 0.35, 0.2)]:
            yield [logo, prod, t, cta], list(w)


# --------------------------------------------------------------------------
# Decoration / safety net
# --------------------------------------------------------------------------


def _place_deco(deco: list[dict], text_rects, ctx: Ctx, out: dict, sw, sh):
    k = min(ctx.tw / sw, ctx.th / sh)
    for e in deco:
        w0, h0 = e["width"], e["height"]
        cap = 0.45 * ctx.mn
        sc = min(k, cap / max(w0, h0)) if max(w0, h0) * k > cap else k
        w, h = w0 * sc, h0 * sc
        cx, cy = (e["x"] + w0 / 2) / sw, (e["y"] + h0 / 2) / sh
        left, top = cx < 0.5, cy < 0.5
        x = 0.0 if left else ctx.tw - w
        y = 0.0 if top else ctx.th - h
        for _ in range(8):
            if not any(_overlap((x, y, w, h), t, 2) for t in text_rects):
                break
            w, h = w * 0.8, h * 0.8
            x = 0.0 if left else ctx.tw - w
            y = 0.0 if top else ctx.th - h
        out[e["id"]] = {"x": x, "y": y, "width": w, "height": h}


def _clamp(el: dict, tw: float, th: float) -> None:
    rot = el.get("rotation") or 0
    bx, by, bw, bh = _rot_bbox(el["x"], el["y"], el["width"], el["height"], rot)
    if bw > tw or bh > th:
        f = min(tw / bw, th / bh)
        cx, cy = el["x"] + el["width"] / 2, el["y"] + el["height"] / 2
        el["width"] *= f
        el["height"] *= f
        el["x"], el["y"] = cx - el["width"] / 2, cy - el["height"] / 2
        if el["type"] == "text":
            el["font_size"] = max(1.0, el["font_size"] * f)
        bx, by, bw, bh = _rot_bbox(el["x"], el["y"], el["width"], el["height"], rot)
    dx = -bx if bx < 0 else (tw - (bx + bw) if bx + bw > tw else 0.0)
    dy = -by if by < 0 else (th - (by + bh) if by + bh > th else 0.0)
    el["x"] += dx
    el["y"] += dy
    el["x"] = min(max(el["x"], 0.0), max(0.0, tw - el["width"]))
    el["y"] = min(max(el["y"], 0.0), max(0.0, th - el["height"]))


def _fallback(source: dict, target: dict) -> dict:
    """Aspect-preserving 'contain' scale + centre; background stretched. Always valid."""
    sw, sh = source["canvas"]["width"], source["canvas"]["height"]
    tw, th = target["width"], target["height"]
    s = min(tw / sw, th / sh)
    ox, oy = (tw - sw * s) / 2, (th - sh * s) / 2
    els = []
    for e in source["elements"]:
        e = copy.deepcopy(e)
        if e["width"] * e["height"] >= 0.85 * sw * sh:
            e.update(x=0.0, y=0.0, width=float(tw), height=float(th))
        else:
            e["x"], e["y"] = ox + e["x"] * s, oy + e["y"] * s
            e["width"], e["height"] = e["width"] * s, e["height"] * s
            if e["type"] == "text":
                e["font_size"] = max(1.0, e["font_size"] * s)
        _clamp(e, tw, th)
        els.append(e)
    return {"canvas": {"width": tw, "height": th}, "elements": els}


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    try:
        return _resize(source, target_canvas)
    except Exception:  # never crash a scoring run
        return _fallback(source, target_canvas)


def _has_text(b: Blk) -> bool:
    if isinstance(b, TextBlk):
        return True
    if isinstance(b, ClusterBlk):
        return bool(b.texts)
    if isinstance(b, RowBlk):
        return any(_has_text(k) for k in b.kids)
    return False


def _resize(source: dict, target_canvas: dict) -> dict:
    sw, sh = float(source["canvas"]["width"]), float(source["canvas"]["height"])
    tw, th = int(target_canvas["width"]), int(target_canvas["height"])
    ctx = Ctx(sw, sh, float(tw), float(th))
    P = _parse(source, ctx)
    blocks = P.blocks
    ctx.bginfo = BgInfo(P.bg, float(tw), float(th))

    best, best_sc, best_blocks, best_panels = None, -1e9, blocks, {}
    for vi, (vblocks, vpanels, vgap) in enumerate(P.variants):
        ctx.gap0 = vgap
        for assign, weights in _candidates(vblocks):
            c = _build(assign, weights, ctx)
            in_cols = {id(b) for col in assign for b in col}
            c["dropped"] += [b for b in vblocks if id(b) not in in_cols]
            sc = _score(c, vblocks, ctx) - (0.02 if vi else 0.0)
            if sc > best_sc + 1e-9:
                best, best_sc, best_blocks, best_panels = c, sc, vblocks, vpanels
    assert best is not None

    out = dict(best["out"])
    # collapsed (dropped) blocks -> invisible specks in the bottom margin
    cur_x = 2.0
    slot_h = max(2.0, ctx.margin - 3.0)
    for b in best["dropped"]:
        w = b.tiny(ctx, min(cur_x, max(0.0, tw - 62.0)), th - slot_h - 1.0, slot_h, out)
        cur_x += w + 2.0

    pad = max(8.0, 0.03 * ctx.mn)
    for pid, mids in best_panels.items():  # exploded card: wrap its placed members in the backdrop
        rs = [(out[i]["x"], out[i]["y"], out[i]["width"], out[i]["height"]) for i in mids if i in out and out[i]["width"] >= 6 and out[i].get("font_size", 99) > 3]
        pe = next(e for e in source["elements"] if e["id"] == pid)
        if rs:
            ux, uy, uw, uh = _union(rs)
            x0, y0 = max(0.0, ux - pad), max(0.0, uy - pad)
            x1, y1 = min(float(tw), ux + uw + pad), min(float(th), uy + uh + pad)
            out[pid] = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
            if pe.get("corner_radius"):
                out[pid]["corner_radius"] = min(pe["corner_radius"], (y1 - y0) / 2)
        else:
            out[pid] = {"x": 2.0, "y": th - 4.0, "width": 4.0, "height": 2.0}
    for e in P.bg:  # background: always full-bleed
        out[e["id"]] = {"x": 0.0, "y": 0.0, "width": float(tw), "height": float(th)}
    text_rects = [r[:4] for r in best["rects"] if _has_text(r[4])]
    blocks = best_blocks
    _place_deco(P.deco, text_rects, ctx, out, sw, sh)

    bg_ids = {b["id"] for b in P.bg}
    els = []
    for e in source["elements"]:
        e = copy.deepcopy(e)
        upd = out.get(e["id"])
        if upd is None:
            s = min(tw / sw, th / sh)
            upd = {"x": e["x"] * s, "y": e["y"] * s, "width": e["width"] * s, "height": e["height"] * s}
            if e["type"] == "text":
                upd["font_size"] = max(1.0, e["font_size"] * s)
        e.update(upd)
        if e["id"] in bg_ids and e["type"] == "image" and e.get("fit") != "cover":
            e["fit"] = "cover"
        _clamp(e, tw, th)
        for k in ("x", "y", "width", "height", "font_size", "corner_radius", "stroke_width"):
            if k in e and isinstance(e[k], (int, float)):
                e[k] = round(float(e[k]), 2)
        els.append(e)
    return {"canvas": {"width": tw, "height": th}, "elements": els}
