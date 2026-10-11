#!/usr/bin/env python3
"""把任意气象要素 GeoTIFF 渲染为科研级 HTML 预览（Leaflet + 高德/ CartoDB 底图 + 配色栅格 + 边界 + 图例 + 指北针 + 比例尺）。

- 配色由 --colormap（逗号分隔的十六进制调色板）决定，按要素自适应；
  未指定时回退到通用暖色色带 YlOrRd_t。单位/图例标题由 --units / --legend 控制。
- 底图默认「高德地图」(AMap, GCJ-02)，要素/边界为 WGS84，叠加前统一转 GCJ-02 对齐；
  可加 --basemap carto 切回 CartoDB（WGS84）。
- 叠加省界矢量（GeoJSON），深色描边、浅填充；图例为连续渐变条 + 极值/单位刻度；
  右上指北针；左下图形比例尺；左上极简标题与统计。
- 边界 GeoJSON 与配色 PNG 均内联进 HTML，仅底图瓦片走网络；Leaflet 已内置自包含。

用法: python make_html.py <data.tif> <boundary.json> <out.html>
      [--title 安徽省] [--period "2023年 平均气温"] [--units °C]
      [--legend "2m 气温"] [--colormap "#313695,#4575b4,..."]
      [--basemap amap|carto] [--bg white] [--no-convert]
"""
import sys
import json
import math
import base64
import numpy as np
import rasterio
from PIL import Image
from pathlib import Path

from geo_utils import (convert_geom, convert_geom_to_gcj02, wgs84_to_gcj02)
from variables import colorize, nice_range, CMAPS

OPT_WITH_VALUE = ("--title", "--period", "--units", "--legend", "--colormap",
                  "--bg", "--basemap", "--cmap-type", "--center", "--zero-anchored")

ASSETS = Path(__file__).parent.parent / "assets"


def _load_leaflet():
    """优先本地内置 leaflet；缺失时返回 (None, None) 回退 CDN 标签。"""
    css_p, js_p = ASSETS / "leaflet.css", ASSETS / "leaflet.js"
    if css_p.exists() and js_p.exists():
        return css_p.read_text(encoding="utf-8"), js_p.read_text(encoding="utf-8")
    return None, None


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


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 3:
        print(__doc__)
        sys.exit(1)
    tif, boundary, out_html = pos[0], pos[1], pos[2]
    title = opts.get("title", "")
    period = opts.get("period", "")
    units = opts.get("units", "")
    legend = opts.get("legend", "变量")
    bg = opts.get("bg", "transparent")
    do_convert = "no-convert" not in flags

    # 调色板：--colormap 优先级最高，否则用变量无关的通用色带。
    # 不写死某个色带名：预设增删色带后硬引用会直接 KeyError（v1.4.0 实测踩过）。
    cmap_hex = opts.get("colormap")
    if cmap_hex:
        hex_list = [h.strip() for h in cmap_hex.split(",") if h.strip()]
    else:
        hex_list = CMAPS.get("YlOrRd_t") or CMAPS[next(iter(CMAPS))]
    if not hex_list:
        print("错误：未找到可用色带。")
        sys.exit(2)
    grad = ",".join(hex_list)

    with rasterio.open(tif) as ds:
        arr = ds.read(1).astype(np.float32)
        nd = ds.nodata
        transform = ds.transform
        bounds = ds.bounds
        tags = ds.tags()
    units = units or tags.get("units", "")
    invalid = (~np.isfinite(arr)) if (nd is None or (isinstance(nd, float) and np.isnan(nd))) \
        else ((arr == nd) | ~np.isfinite(arr))
    invalid |= arr <= -32767
    valid = arr[~invalid]
    if valid.size == 0:
        print("警告：要素 TIF 全为无效值（nodata），无法生成配色预览。"
              "请确认输入数据含有效格点（如网络请求是否全部失败）。")
        sys.exit(2)
    vmin_data, vmax_data = float(valid.min()), float(valid.max())

    # 图例区间：优先用编排器显式传来的 --cmap-type/--center（变量感知、最准）；
    # 缺省时按单位启发（°C/hPa 发散、其余顺序），与旧行为兼容。
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

    # 渲染配色 PNG：先对边界附近无效像元做少量插值填充，避免上采样时
    # 与透明区（0.0）混合出现蓝边；然后双线性上采样到 ~900px 长边。
    # 注意：alpha 与覆盖率统计必须使用「填充前」的有效掩膜（invalid0），
    # 否则对小尺寸城市栅格，fillnodata 的大半径会把边界外全部填满，
    # 导致 alpha 全 255（地图变成整块矩形、不再按行政边界裁切）且覆盖率虚高。
    h, w = arr.shape
    invalid0 = invalid.copy()  # 填充前的原始有效掩膜（用于 alpha / 覆盖率）
    if invalid.any() and (~invalid).any():
        from rasterio.fill import fillnodata
        filled = fillnodata(arr, mask=~invalid, max_search_distance=5, smoothing_iterations=0)
        arr = np.where(invalid, filled, arr)
        # 注意：不更新 invalid —— alpha 与统计仍用 invalid0
    target = 900
    factor = target / max(w, h)
    if factor >= 1:
        W, H = max(1, int(round(w * factor))), max(1, int(round(h * factor)))
        interp = Image.BILINEAR
    else:
        W, H = max(1, int(round(w * factor))), max(1, int(round(h * factor)))
        interp = Image.LANCZOS
    arr_filled = np.nan_to_num(arr, nan=0.0)
    up = np.array(Image.fromarray(arr_filled).resize((W, H), interp), dtype=np.float32)
    mask_up = np.array(Image.fromarray((~invalid0).astype("uint8") * 255)
                       .resize((W, H), Image.NEAREST), dtype=np.uint8) > 127
    rgb = colorize(up, _cmap_name_or_hex(hex_list), vmin, vmax)
    alpha = np.where(mask_up, 255, 0).astype(np.uint8)
    if bg == "white":
        rgb[~mask_up] = (255, 255, 255)
        img = Image.fromarray(rgb, "RGB")
    else:
        img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    png_path = str(out_html).rsplit(".", 1)[0] + "_preview.png"
    img.save(png_path)

    # 自包含：把配色 PNG 与 Leaflet 都内联进 HTML，避免相对路径/运行时联网问题。
    png_bytes = Path(png_path).read_bytes()
    png_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    lf_css, lf_js = _load_leaflet()
    leaflet_css_tag = (f"<style>{lf_css}</style>"
                       if lf_css else
                       '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>')
    leaflet_js_tag = (f"<script>{lf_js}</script>"
                     if lf_js else
                     '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>')

    basemap = (opts.get("basemap") or "amap").lower()
    amap = basemap == "amap"

    gj = json.load(open(boundary, encoding="utf-8"))
    feats = gj.get("features", [{"geometry": gj, "properties": {}}])
    if amap:
        if "no-convert" in flags:
            for f in feats:
                if f.get("geometry"):
                    f["geometry"] = convert_geom_to_gcj02(f["geometry"])
    else:
        if do_convert:
            for f in feats:
                if f.get("geometry"):
                    f["geometry"] = convert_geom(f["geometry"])
    gj = {"type": "FeatureCollection", "features": feats}

    ow = (bounds.left, bounds.bottom)
    oe = (bounds.right, bounds.top)
    if amap:
        ow = wgs84_to_gcj02(*ow)
        oe = wgs84_to_gcj02(*oe)
    o_minx, o_miny, o_maxx, o_maxy = ow[0], ow[1], oe[0], oe[1]

    dx_m = abs(transform.a) * 111320.0 * math.cos(math.radians((bounds.top + bounds.bottom) / 2))
    dy_m = abs(transform.e) * 110574.0
    nodata_pct = 100.0 * invalid0.mean()

    img_w, img_h = img.size
    span = vmax - vmin
    fmt = (".2f" if span < 2 else ".1f" if span < 20 else ".0f")
    ticks = [vmin + span * f for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    unit_suf = f" {units}" if units else ""

    if amap:
        tile_layer_js = (
            "L.tileLayer('https://wprd0{s}.is.autonavi.com/appmaptile"
            "?lang=zh_cn&size=1&style=7&x={x}&y={y}&z={z}',\n"
            "  {maxZoom:18, subdomains:'1234', "
            "attribution:'© 高德地图 AutoNavi © GS(2023)....'}).addTo(map);")
    else:
        tile_layer_js = (
            "L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',\n"
            "  {maxZoom:18, subdomains:'abcd',"
            " attribution:'© OpenStreetMap contributors © CARTO'}).addTo(map);")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title or '气象要素可视化'}</title>
{leaflet_css_tag}
<style>
  html,body{{margin:0;height:100%;font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#0e1116;}}
  #map{{position:absolute;inset:0;background:#dfe6ee;}}
  .panel{{position:absolute;z-index:1000;background:rgba(255,255,255,.94);border:1px solid #d4dae0;
    border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.18);padding:10px 13px;color:#1c2733;}}
  #title{{top:14px;left:14px;max-width:300px;}}
  #title h1{{margin:0;font-size:16px;font-weight:700;letter-spacing:.5px;}}
  #title .cap{{margin-top:3px;font-size:12px;color:#5a6776;}}
  #stats{{margin-top:8px;font-size:12px;line-height:1.7;color:#33414f;border-top:1px dashed #d4dae0;padding-top:6px;}}
  #stats b{{color:#1c2733;font-variant-numeric:tabular-nums;}}
  #legend{{left:14px;bottom:26px;width:210px;}}
  #legend .ttl{{font-size:12px;font-weight:600;margin-bottom:5px;color:#1c2733;}}
  #legend .bar{{height:13px;border-radius:3px;border:1px solid #b9c2cc;
    background:linear-gradient(90deg,{grad});}}
  #legend .ticks{{display:flex;justify-content:space-between;font-size:11px;color:#44515f;margin-top:3px;
    font-variant-numeric:tabular-nums;}}
  #legend .unit{{font-size:11px;color:#5a6776;margin-top:2px;}}
  #legend .lnrow{{font-size:11px;color:#44515f;margin-top:7px;display:flex;align-items:center;}}
  #legend .ln{{display:inline-block;width:18px;height:0;border-top:2px solid #1c2733;margin-right:6px;}}
  #north{{top:14px;right:14px;width:60px;height:60px;padding:0;border:none;background:transparent;box-shadow:none;}}
  .lbl{{font-size:11px;fill:#33414f;font-weight:600;}}
</style>
</head>
<body>
<div id="map"></div>
<div id="title" class="panel">
  <h1>{title or '气象要素分布'}</h1>
  <div class="cap">{period}</div>
  <div id="stats">
    均值 <b>{mean:.2f}</b>{unit_suf}　范围 <b>{vmin_data:.2f}</b>~<b>{vmax_data:.2f}</b>{unit_suf}<br/>
    有效覆盖 <b>{100-nodata_pct:.1f}</b>%
  </div>
</div>
<div id="legend" class="panel">
  <div class="ttl">{legend}{unit_suf}</div>
  <div class="bar"></div>
  <div class="ticks"><span>{ticks[0]:{fmt}}</span><span>{ticks[1]:{fmt}}</span><span>{ticks[2]:{fmt}}</span><span>{ticks[3]:{fmt}}</span><span>{ticks[4]:{fmt}}</span></div>
  <div class="lnrow"><span class="ln"></span>行政边界</div>
</div>
<div id="north" class="panel">
  <svg viewBox="0 0 60 60" width="60" height="60">
    <circle cx="30" cy="30" r="27" fill="rgba(255,255,255,.92)" stroke="#9aa7b4"/>
    <circle cx="30" cy="30" r="27" fill="none" stroke="#c8d0d8" stroke-width="0.6"/>
    <g stroke="#7c8895" stroke-width="0.8">
      <line x1="30" y1="3" x2="30" y2="9"/><line x1="30" y1="51" x2="30" y2="57"/>
      <line x1="3" y1="30" x2="9" y2="30"/><line x1="51" y1="30" x2="57" y2="30"/>
    </g>
    <polygon points="30,7 34,30 30,27 26,30" fill="#c0392b"/>
    <polygon points="30,53 26,30 30,33 34,30" fill="#34495e"/>
    <circle cx="30" cy="30" r="2" fill="#34495e"/>
    <text x="30" y="15" text-anchor="middle" class="lbl" style="font-size:9px;fill:#c0392b">N</text>
  </svg>
</div>
{leaflet_js_tag}
<script>
try {{
var bounds = [[{o_miny:.5f}, {o_minx:.5f}], [{o_maxy:.5f}, {o_maxx:.5f}]];
var map = L.map('map', {{center:[(bounds[0][0]+bounds[1][0])/2,(bounds[0][1]+bounds[1][1])/2],
  zoom:6, attributionControl:false}});
{tile_layer_js}
L.imageOverlay('{png_uri}', bounds, {{opacity:0.92}}).addTo(map);
var boundary = {json.dumps(gj, ensure_ascii=False)};
L.geoJSON(boundary, {{style:{{color:'#1c2733', weight:1.6, fillColor:'#1c2733', fillOpacity:0.04}}}}).addTo(map);
L.control.scale({{imperial:false, position:'bottomright', maxWidth:160}}).addTo(map);
map.fitBounds(bounds);
}} catch (err) {{
  var tp = document.getElementById('title');
  if (tp) tp.insertAdjacentHTML('beforeend',
    '<div class="cap" style="color:#c0392b">渲染出错：' + (err && err.message ? err.message : err) + '</div>');
  console.error(err);
}}
</script>
</body>
</html>"""
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"配色栅格: {png_path} ({img_w}×{img_h})")
    print(f"预览 HTML: {out_html}")
    print(f"要素范围 {vmin_data:.2f}~{vmax_data:.2f}{unit_suf}，图例区间 [{vmin:.0f},{vmax:.0f}]")


def _cmap_name_or_hex(hex_list):
    """colorize 接受调色板名称或十六进制列表；这里统一把列表转成可识别输入。

    为复用 variables.colorize 的 CMAPS 查表路径，若传入的是已展开的 hex 列表，
    直接在 variables 中临时注册（以 '_custom' 名为键），避免每次重复构造。
    """
    from variables import CMAPS
    key = "_runtime"
    CMAPS[key] = hex_list
    return key


if __name__ == "__main__":
    main()
