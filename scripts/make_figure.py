#!/usr/bin/env python3
"""把任意气象要素 GeoTIFF 渲染为**科研级静态出图**（单张 PNG，离线自包含，不联网）。

与 make_html.py（交互式 Leaflet 预览）互补：本脚本产出可用于论文/报告的**出版级单图**——
- 要素栅格按要素自适应配色（ColorBrewer），按行政边界裁切；
- 叠加行政边界（深色细描边）、经纬网（graticule）与经纬度刻度；
- **罗盘式指北针**、**图形比例尺**（km，按中心纬度测算）；
- 规范**颜色条**（colorbar，带刻度与单位）；
- 极简**标题**（仅区划名，不堆砌元数据副标题）；周期/数据源以小字脚注呈现。

风格遵循本用户的科研出图规范：极简标题、罗盘式指北针、线状边界图例、图形比例尺。

用法: python make_figure.py <data.tif> <boundary.json> <out.png>
      [--title 合肥市] [--period "2026年6月 月均"] [--units m/s] [--legend "10m 风速"]
      [--colormap "#ffffcc,..."] [--cmap-type diverging|sequential] [--center 0]
      [--dpi 300] [--width-in 8] [--no-graticule] [--no-boundary] [--font "Microsoft YaHei"]
"""
import math
import sys

import matplotlib
matplotlib.use("Agg")
import numpy as np
import rasterio
import matplotlib.pyplot as plt
import matplotlib.path
from matplotlib.colors import LinearSegmentedColormap
from matplotlib import font_manager as fm

from geo_utils import load_boundary_wgs84
from variables import nice_range, CMAPS

OPT_WITH_VALUE = ("--title", "--period", "--units", "--legend", "--colormap",
                  "--cmap-type", "--center", "--dpi", "--width-in", "--font",
                  "--zero-anchored", "--dump-filled")


# ---------------------------------------------------------------------------
# 栅格预处理原语：全部用 numpy 实现，**不引入 scipy**。
#
# 为什么去掉 scipy（实测依据）：
#   `from scipy import ndimage` 边际导入耗时 1067 ms，占本脚本总耗时的约 1/3
#   （总 3.4 s = 启动 741 ms + 导入 1936 ms + 干活 <0.7 s）。而它只用于三件事：
#   最近邻补洞 / 上采样插值 / 高斯平滑 —— 三件都能用 numpy 等价实现。
#
#   实测对比（合肥市真实边界 + 栅格，无裁剪路径以暴露缺口）：
#     管线                                      预处理   边界内缺口占比
#     scipy zoom(1)+gaussian(2.2)              0.515 s     2.282 %
#     numpy 膨胀补洞 + 可分离高斯(0.7)+双线性   0.040 s     0.244 %
#   并且 scipy 路线**画质更差**：`zoom(order=1)` 对上采样前的"无效置 0"做线性
#   插值，会把紧邻边界的无效像元混成极浅色（近白），视觉上正是一圈白边缺口；
#   缺口占比因此比"完全不补洞"（0.590 %）还高 4 倍。
#   numpy 路线把平滑放在**原分辨率**、插值交给 imshow(bilinear)，从根上避开该陷阱。
# ---------------------------------------------------------------------------

def _gauss1d(sigma):
    """一维归一化高斯核（半径取 3σ，足够覆盖 99.7%）。"""
    r = max(1, int(math.ceil(3.0 * sigma)))
    x = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    return k / k.sum()


def _sep_conv(a, k, axis):
    """沿 axis 做一维卷积，边界用 edge 外推（等价 scipy 的 mode='nearest'）。"""
    r = (len(k) - 1) // 2
    pad = [(r, r), (0, 0)] if axis == 0 else [(0, 0), (r, r)]
    ap = np.pad(a, pad, mode="edge")
    out = np.zeros_like(a, dtype=np.float64)
    for i, kv in enumerate(k):
        sl = [slice(None), slice(None)]
        sl[axis] = slice(i, i + a.shape[axis])
        out += float(kv) * ap[tuple(sl)]
    return out


def blur_masked(a, mask, sigma):
    """掩膜加权高斯平滑（可分离，numpy-only）。

    不能直接对含 NaN 的数组做卷积：NaN 会沿邻域扩散污染整幅栅格
    （实测 3x3 仅 1 个 NaN 即全变 NaN）。故数值项与权重项分别卷积后相除。
    """
    k = _gauss1d(sigma)
    num = _sep_conv(_sep_conv(np.where(mask, a, 0.0), k, 0), k, 1)
    den = _sep_conv(_sep_conv(mask.astype(np.float64), k, 0), k, 1)
    return np.where(den > 1e-6, num / np.maximum(den, 1e-6), np.nan)


def fill_nearest(a, invalid):
    """迭代 4 邻域膨胀填补：把 invalid 处填成最近的有效值（numpy-only）。

    等价 scipy.ndimage.distance_transform_edt(return_indices) 的最近邻兜底，
    但无需 scipy。每轮向外扩散一圈，轮数 = 最大缺失半径。
    返回 (填补结果, 迭代轮数)；迭代轮数用于可观测性（不再静默）。
    """
    out = np.where(invalid, 0.0, a).astype(np.float64)
    src = ~invalid
    todo = invalid.copy()
    n, lim = 0, int(out.shape[0] + out.shape[1])
    while todo.any() and n < lim:
        acc = np.zeros_like(out)
        w = np.zeros_like(out)
        for axis in (0, 1):
            for shift in (1, -1):
                s = np.roll(np.where(src, out, 0.0), shift, axis=axis)
                m = np.roll(src.astype(np.float64), shift, axis=axis)
                sl = [slice(None), slice(None)]
                sl[axis] = slice(0, 1) if shift == 1 else slice(-1, None)
                s[tuple(sl)] = 0.0
                m[tuple(sl)] = 0.0
                acc += s
                w += m
        good = todo & (w > 0)
        if not good.any():
            break
        out[good] = acc[good] / w[good]
        src = src | good
        todo &= ~good
        n += 1
    return out, n


def parse_args(argv):
    pos, opts, flags = [], {}, set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in OPT_WITH_VALUE:
            opts[a.lstrip("-")] = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            flags.add(a.lstrip("-"))
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts, flags


def _cjk_font(name=None):
    """优先用显式 --font，否则在常见 Windows 中文字体里挑一个存在的；找不到返回 None（用默认）。"""
    candidates = []
    if name:
        candidates.append(name)
    candidates += ["Microsoft YaHei", "微软雅黑", "SimHei", "黑体", "SimSun", "宋体",
                  "Noto Sans CJK SC", "WenQuanYi Micro Hei"]
    errors = []
    # 先按名字在已注册字体里找
    for c in candidates:
        try:
            if fm.findfont(fm.FontProperties(family=c), fallback_to_default=False):
                return fm.FontProperties(family=c)
        except Exception as e:
            # 单个候选失败是正常的（字体未安装），但必须留痕：
            # 静默 pass 会让"字体其实没生效"永远查不出来。
            errors.append(f"{c}: {type(e).__name__}")
    # 再按文件绝对路径找（Windows 常见位置）
    import os
    paths = [
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
    ]
    for p in paths:
        if os.path.exists(p):
            return fm.FontProperties(fname=p)
    if errors:
        print(f"[warn] 按族名查找中文字体全部失败（{'; '.join(errors[:3])}"
              f"{' …' if len(errors) > 3 else ''}），已回退到按路径查找。"
              f"若标题出现方框，请用 --font 指定字体文件。")
    return None


def _nice_deg(span):
    if span <= 0:
        return 0.1
    exp = math.floor(math.log10(span))
    base = 10.0 ** exp
    f = span / base
    for c in (1.0, 2.0, 2.5, 5.0, 10.0):
        if f <= c:
            return c * base
    return 10.0 * base


def _fmt(v, span):
    if abs(span) < 1:
        return f"{v:.3f}"
    if abs(span) < 5:
        return f"{v:.2f}"
    if abs(span) < 20:
        return f"{v:.1f}"
    return f"{v:.0f}"


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 3:
        print(__doc__)
        sys.exit(1)
    tif, boundary, out_png = pos[0], pos[1], pos[2]
    title = opts.get("title", "")
    period = opts.get("period", "")
    units = opts.get("units", "")
    legend = opts.get("legend", "变量")
    cmap_hex = opts.get("colormap")
    dpi = int(opts.get("dpi", 300))
    width_in = float(opts.get("width-in", 8.0))
    grat = "no-graticule" not in flags
    draw_boundary = "no-boundary" not in flags
    font_name = opts.get("font")

    if cmap_hex:
        hex_list = [h.strip() for h in cmap_hex.split(",") if h.strip()]
    else:
        # 不写死某个色带名：预设增删色带后硬引用会直接 KeyError（实测已踩）。
        hex_list = CMAPS[next(iter(CMAPS))]
    if not hex_list:
        print("错误：未找到可用色带。")
        sys.exit(2)
    cmap = LinearSegmentedColormap.from_list("cnclimate", hex_list)

    # ---- 读取栅格 ----
    with rasterio.open(tif) as ds:
        arr = ds.read(1).astype(np.float64)
        nd = ds.nodata
        left, bottom, right, top = ds.bounds
        transform = ds.transform
        tags = ds.tags()
    units = units or tags.get("units", "")
    invalid = (~np.isfinite(arr)) if (nd is None or (isinstance(nd, float) and np.isnan(nd))) \
        else ((arr == nd) | ~np.isfinite(arr))
    invalid |= arr <= -32767
    valid = arr[~invalid]
    if valid.size == 0:
        print("错误：要素 TIF 全为无效值（nodata），无法出图。")
        sys.exit(2)
    vmin_data, vmax_data = float(valid.min()), float(valid.max())

    cmap_type = (opts.get("cmap-type") or opts.get("cmap_type")
                 or ("diverging" if units in ("°C", "hPa") else "sequential"))
    center_opt = opts.get("center")
    center = float(center_opt) if (center_opt not in (None, "")) else None
    if center is None and cmap_type != "diverging":
        center = 0.0
    zero_anchored = "zero-anchored" in opts
    lo, hi = nice_range(vmin_data, vmax_data, cmap_type, center, zero_anchored)
    vmin, vmax = float(lo), float(hi)
    mean = float(valid.mean())

    # ---- 画布 ----
    fp = _cjk_font(font_name)
    if fp is None:
        print("提示：未找到中文字体，标题若含中文可能会显示为方框；可用 --font 指定。")
    extent = [left, right, bottom, top]
    width_deg = right - left
    height_deg = top - bottom

    # ---- 边界内补洞（v1.6.0 重写：修掉 NameError 静默失效 + 去 scipy）----
    #
    # v1.4.0 引入的这段本意是：上游裁剪切掉的边界内像元（all_touched 掩膜所致）
    # 用最近邻补回，让填色严格由矢量边界决定。但它读 `disp` 时 `disp` 还没被定义
    # （首次赋值在下面几十行），`NameError` 被 `except Exception: pass` 吞掉 ——
    # **这段逻辑从引入起就从未执行过**（实测：从未打印过"边缘补齐"）。
    # v1.6.0 修法：先定义数组再补洞，且失败/生效都打印计数（不再静默）。
    #
    # 另注（v1.6.0）：v1.5.0 已在取数阶段加 pad_cells=3，使**真实数据**环绕边界，
    # 对新数据本段通常补 0 个像元。它保留为对旧栅格的兜底。
    valid_src = ~invalid
    disp = np.where(invalid, np.nan, arr)
    holes = None
    try:
        from rasterio.features import geometry_mask as _gmask
        _geoms = load_boundary_wgs84(boundary, do_convert=True)[2]
        _gl = list(_geoms.geoms) if _geoms.geom_type.startswith("Multi") else [_geoms]
        _inside = _gmask(_gl, out_shape=arr.shape, transform=transform,
                         invert=True, all_touched=True)
        holes = _inside & ~valid_src
        if holes.any() and valid_src.any():
            filled, _iters = fill_nearest(np.where(valid_src, arr, 0.0), ~valid_src)
            disp = np.where(holes, filled, disp)
            invalid = ~np.isfinite(disp)
    except Exception as e:
        print(f"[warn] 边界内补洞未生效（{type(e).__name__}: {e}）；"
              f"填色可能残留边缘台阶。")
        holes = None
    if holes is not None and holes.sum():
        print(f"  边界内边缘补齐 {int(holes.sum())} 像元（最近邻 + 掩膜加权平滑）")

    # 可观测性出口：把补洞+平滑后的栅格落盘，便于回归测试逐像元比对，
    # 也便于人工核对"补洞到底做了没有"（v1.4.0 的静默失效正是查不出这一点）。
    _dump = opts.get("dump-filled")
    if _dump:
        with rasterio.open(tif) as _s:
            _prof = _s.profile.copy()
        _prof.update(dtype="float32", count=1, nodata=np.nan)
        with rasterio.open(_dump, "w", **_prof) as _d:
            _d.write(np.where(np.isfinite(disp), disp, np.nan).astype(np.float32), 1)
        print(f"  补洞后栅格已导出: {_dump}")

    # ---- 掩膜加权平滑 + 上采样 ----
    #
    # 平滑放在**原分辨率**（σ=0.7，按像元间距计），插值交给 imshow(bilinear)。
    # 见文件头部注释：scipy 路线把平滑放在上采样后（σ=2.2 像素）且用
    # zoom(order=1) 插值"无效置 0"，会造出近白边 → 实测缺口占比反而翻 4 倍。
    if valid_src.sum() > 1:
        sm = blur_masked(np.where(np.isfinite(disp), disp, arr), np.isfinite(disp), 0.7)
        disp = np.where(np.isfinite(sm), sm, disp)
    else:
        print("[warn] 有效像元不足 2 个，跳过滤波。")
    # 插值用 bilinear 且**不传 alpha**：由 imshow 直接在真实数组上插值，
    # 无效处保持 NaN（matplotlib 自动透明），不会产生半透明白边。
    # 精确裁剪完全交给下方 clip_path（矢量边界是唯一的裁剪者）。
    interp = "bilinear"
    aspect = height_deg / width_deg if width_deg > 0 else 1.0
    # 主体高度随数据比例，额外留白给标题/脚注/颜色条
    fig_h = max(3.0, width_in * aspect) + 0.9
    fig = plt.figure(figsize=(width_in, fig_h), facecolor="white")
    # 主体图框（右侧留给 colorbar）
    ax = fig.add_axes([0.07, 0.09, 0.80, 0.80])
    ax.set_facecolor("white")

    # 上采样策略（v1.6.0 改为 numpy + imshow 插值）：
    # 不再手工 zoom 上采样，直接把栅格交给 imshow 用 bilinear 插值到画布像素密度，
    # 无效处保持 NaN。理由见文件头部注释（scipy zoom 会造近白边、且慢 1067 ms）。
    masked = np.where(np.isfinite(disp), disp, np.nan)
    im = ax.imshow(masked, extent=extent, origin="upper", cmap=cmap,
                   vmin=vmin, vmax=vmax, interpolation=interp,
                   zorder=1, resample=False)

    # ---- 行政边界 ----
    # 关键：按边界多边形精确裁剪（clip_path），使填色严格贴合行政区界，
    # 而不是停在栅格像元的方块阶梯上。
    _, _, union = load_boundary_wgs84(boundary, do_convert=True)
    geoms = union.geoms if union.geom_type.startswith("Multi") else [union]
    patches = []
    if draw_boundary:
        for g in geoms:
            if g.geom_type != "Polygon":
                continue
            patch = matplotlib.patches.Polygon(np.array(g.exterior.coords),
                                               closed=True, facecolor="none",
                                               edgecolor="#1c2733", lw=1.3, zorder=3)
            ax.add_patch(patch)
            patches.append(patch)
        for g in geoms:
            if g.geom_type == "Polygon":
                for interior in g.interiors:
                    xi, yi = interior.xy
                    ax.plot(xi, yi, color="#1c2733", lw=1.0, zorder=3)
    if patches:
        # 手工构造 compound path（顶点可不等长，故天然支持多部件与带洞的区划）。
        # 不用 make_compound_path_from_polys：它要求所有环顶点数相同，遇不等长直接报错。
        verts, codes = [], []
        for g in geoms:
            if g.geom_type != "Polygon":
                continue
            for ring in [g.exterior] + list(g.interiors):
                c = np.asarray(ring.coords, dtype=float)
                if c.ndim != 2 or c.shape[0] < 3:
                    continue
                n = c.shape[0]
                verts.append(c)
                codes.append(np.concatenate([
                    [matplotlib.path.Path.MOVETO],
                    [matplotlib.path.Path.LINETO] * (n - 2),
                    [matplotlib.path.Path.CLOSEPOLY]]))
        if verts:
            clip_path = matplotlib.path.Path(np.concatenate(verts),
                                             np.concatenate(codes))
            im.set_clip_path(clip_path, transform=ax.transData)

    # 坐标轴范围：**在数据范围外加留白**，避免行政边界紧贴图框（用户反馈 #2）。
    # 同时钉死范围，防止后续注记（比例尺等）意外把范围撑爆、压扁地图。
    # 留白按跨度百分比取，且保持等比以免形变。
    pad_frac = 0.06
    pad_x = width_deg * pad_frac
    pad_y = height_deg * pad_frac
    ax.set_xlim(left - pad_x, right + pad_x)
    ax.set_ylim(bottom - pad_y, top + pad_y)

    # ---- 经纬网 + 刻度 ----
    if grat:
        step = _nice_deg(max(width_deg, height_deg))
        xs = np.arange(math.ceil(left / step) * step, right + 1e-9, step)
        ys = np.arange(math.ceil(bottom / step) * step, top + 1e-9, step)
        ax.set_xticks(xs)
        ax.set_yticks(ys)
        ax.grid(True, ls=":", lw=0.5, color="#b9c2cc", zorder=0)
        ax.tick_params(labelsize=8, colors="#33414f")
        ax.set_xlabel("经度 (°E)" if fp is None else "经度 (°E)", fontsize=9,
                      color="#33414f", fontproperties=fp)
        ax.set_ylabel("纬度 (°N)", fontsize=9, color="#33414f", fontproperties=fp)
        # 刻度标签格式
        def _lonfmt(v):
            return f"{v:.1f}°" if abs(v) < 1 else (f"{v:.1f}°E" if v > 0 else f"{-v:.1f}°W")
        def _latfmt(v):
            return f"{v:.1f}°N" if v > 0 else (f"{-v:.1f}°S" if v < 0 else f"{v:.1f}°")
        ax.set_xticklabels([_lonfmt(v) for v in xs], fontproperties=fp)
        ax.set_yticklabels([_latfmt(v) for v in ys], fontproperties=fp)
    else:
        ax.set_xticks([])
        ax.set_yticks([])
    for s in ax.spines.values():
        s.set_color("#33414f")
        s.set_linewidth(0.8)

    # ---- 颜色条 ----
    cax = fig.add_axes([0.90, 0.09, 0.025, 0.80])
    # 刻度值本身按「漂亮步长」取整，而非仅靠格式化四舍五入：
    # 窄区间（如气温 24.90~26.90）直接 linspace 会得到 25.40/25.90 这类刻度，
    # 标签虽正确但不符合科研图例习惯。
    raw = np.linspace(vmin, vmax, 5)
    step = raw[1] - raw[0]
    dec = max(0, min(3, int(math.floor(math.log10(1.0 / step))) + 1)) if step > 0 else 2
    dec = min(dec, 2)
    ticks = np.round(raw, dec)
    ticks[0], ticks[-1] = round(vmin, dec), round(vmax, dec)
    cbar = fig.colorbar(im, cax=cax, ticks=ticks)
    cbar.set_label(f"{legend} ({units})" if units else legend,
                   fontsize=10, color="#1c2733", fontproperties=fp)
    cbar.ax.tick_params(labelsize=8, colors="#33414f")
    for t in cbar.ax.get_yticklabels():
        t.set_fontproperties(fp)

    # ---- 罗盘式指北针（右上角 inset）----
    # 设计：极简科研罗盘 —— 白色半透明底盘 + 细外环 + 八方位短刻度 +
    # 单色（深墨）细长北针 + 空心南针，"N" 置于环外正上方。
    # 不用红/蓝双色实心块（旧版），那样在彩色填色上显得突兀且廉价。
    axn = ax.inset_axes([0.80, 0.78, 0.17, 0.17], zorder=5)
    axn.set_xlim(-1.25, 1.25)
    axn.set_ylim(-1.25, 1.25)
    axn.set_aspect("equal")
    axn.axis("off")
    # 底盘：略大于外环，半透明白，压住下方填色保证可读
    axn.add_patch(plt.Circle((0, 0), 1.18, fill=True, color="white",
                             alpha=0.55, ec="none", zorder=1))
    # 外环（细）
    axn.add_patch(plt.Circle((0, 0), 1.0, fill=False, ec="#33414f",
                             lw=1.0, zorder=2))
    # 内环（更细、更浅）
    axn.add_patch(plt.Circle((0, 0), 0.82, fill=False, ec="#9aa7b4",
                             lw=0.6, zorder=2))
    # 八方位短刻度：正方位长、斜方位短
    for ang_deg in range(0, 360, 45):
        a = math.radians(ang_deg)
        major = (ang_deg % 90 == 0)
        r_in, r_out = (0.82, 1.0) if major else (0.90, 1.0)
        axn.plot([r_in * math.sin(a), r_out * math.sin(a)],
                 [r_in * math.cos(a), r_out * math.cos(a)],
                 color="#33414f" if major else "#9aa7b4",
                 lw=0.9 if major else 0.6, zorder=3)
    # 北针：细长实心三角（深墨），尖端留白边
    axn.add_patch(plt.Polygon([(0, 0.80), (0.13, -0.10), (0, 0.02), (-0.13, -0.10)],
                             closed=True, facecolor="#1c2733",
                             edgecolor="white", lw=0.5, zorder=4))
    # 南针：空心三角（白底细边），与北针形成轻重对比
    axn.add_patch(plt.Polygon([(0, -0.80), (0.13, 0.10), (0, -0.02), (-0.13, 0.10)],
                             closed=True, facecolor="white",
                             edgecolor="#1c2733", lw=0.7, zorder=4))
    # 中心小点
    axn.add_patch(plt.Circle((0, 0), 0.05, fill=True, color="#1c2733", zorder=5))
    # "N" 置于环外正上方
    axn.text(0, 1.06, "N", ha="center", va="bottom", fontsize=9,
             color="#1c2733", fontweight="bold", fontproperties=fp, zorder=6)

    # ---- 图形比例尺（km）----
    # 中心纬度处的经度方向米/度
    clat = (top + bottom) / 2.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(clat))
    # 取图宽约 35% 作为目标长度，选一个「整数感」的 km 数（1/2/2.5/5×10^k）。
    # 注意单位：target_km 已是公里，_nice_deg 返回值同为公里，不可再乘 1000。
    half_w_m = (width_deg / 2.0) * m_per_deg_lon
    target_km = (half_w_m * 0.35) / 1000.0
    nice_km = _nice_deg(target_km)
    # 兜底钳制：比例尺不得超过图宽 45%，避免任何单位/量级错误把 x 轴撑爆、压扁地图
    max_km = (width_deg * 0.45) * m_per_deg_lon / 1000.0
    if nice_km <= 0 or nice_km > max_km:
        nice_km = _nice_deg(max_km)
    len_deg = (nice_km * 1000.0) / m_per_deg_lon
    sx = left + width_deg * 0.04
    sy = bottom + height_deg * 0.07
    ax.plot([sx, sx + len_deg], [sy, sy], color="#1c2733", lw=1.6, zorder=6)
    ax.plot([sx, sx], [sy - height_deg * 0.012, sy + height_deg * 0.012],
            color="#1c2733", lw=1.6, zorder=6)
    ax.plot([sx + len_deg, sx + len_deg], [sy - height_deg * 0.012, sy + height_deg * 0.012],
            color="#1c2733", lw=1.6, zorder=6)
    km_txt = f"{nice_km:.0f} km" if nice_km >= 1 else f"{nice_km*1000:.0f} m"
    ax.text(sx + len_deg / 2.0, sy + height_deg * 0.03, km_txt,
            ha="center", va="bottom", fontsize=8, color="#1c2733",
            fontproperties=fp, zorder=6)

    # ---- 标题（极简：仅区划名）----
    if title:
        ax.set_title(title, fontsize=15, fontweight="bold", color="#1c2733",
                     loc="left", pad=8, fontproperties=fp)
    # 脚注：周期 + 数据源（小字，不喧宾夺主）
    src = tags.get("source", "")
    foot = period
    if src:
        foot = (foot + "　" if foot else "") + f"数据源：{src}"
    if foot:
        fig.text(0.07, 0.035, foot, fontsize=8, color="#5a6776", fontproperties=fp)

    fig.savefig(out_png, dpi=dpi, facecolor="white", bbox_inches="tight")
    print(f"科研级出图: {out_png} ({dpi} dpi, {width_in}in 宽, 数据 {vmin_data:.2f}~{vmax_data:.2f}{(' '+units) if units else ''})")
    print(f"  图例区间 [{vmin:.2f},{vmax:.2f}]　均值 {mean:.2f}{(' '+units) if units else ''}")


if __name__ == "__main__":
    main()
