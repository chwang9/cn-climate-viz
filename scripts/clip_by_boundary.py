#!/usr/bin/env python3
"""用行政边界 GeoJSON 掩膜裁切任意栅格，**输出固定为 WGS84 地理坐标（EPSG:4326）**。

DataV 边界为 GCJ-02，气温/影像/要素数据均为 WGS84（EPSG:4326），
默认先把边界反解到 WGS84 再裁切，避免几百米偏移（`--no-convert` 关闭）。

黑边修复要点：边界外强制以 NaN（或 --nodata 指定的值）填充，并可选对
内部空洞做插值补洞，避免成品出现一圈异常低值"黑边"。

默认 all_touched=True（边界上被擦到的像元都保留，避免锯齿状缺口），
加 `--strict` 改为只保留中心落在边界内的像元。可用 `--buffer 500` 外扩边界（米）。

本 skill 只输出地理坐标系：像元为经纬度格网（约 1/3600°，非正方形）。
需要投影坐标系时请在 GIS 软件或下游自行重投影。

选项: --no-convert  --strict  --fill  --nodata <值>  --buffer <米>
"""
import os
import sys
import json
import math
import numpy as np
import rasterio
from rasterio.fill import fillnodata
from rasterio.crs import CRS
from rasterio.warp import calculate_default_transform, reproject, Resampling
from shapely.geometry import shape
from shapely.affinity import scale

GCJ_A = 6378245.0
GCJ_EE = 0.00669342162296594323


def _out_of_china(lng, lat):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _tlat(x, y):
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _tlng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def gcj02_to_wgs84(lng, lat):
    """GCJ-02 -> WGS84，迭代反解，误差 < 1m"""
    if _out_of_china(lng, lat):
        return lng, lat
    wlng, wlat = lng, lat
    for _ in range(3):
        dlat = _tlat(wlng - 105.0, wlat - 35.0)
        dlng = _tlng(wlng - 105.0, wlat - 35.0)
        radlat = wlat / 180.0 * math.pi
        magic = math.sin(radlat)
        magic = 1 - GCJ_EE * magic * magic
        sqrtmagic = math.sqrt(magic)
        dlat = (dlat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtmagic) * math.pi)
        dlng = (dlng * 180.0) / (GCJ_A / sqrtmagic * math.cos(radlat) * math.pi)
        wlng = lng - dlng
        wlat = lat - dlat
    return wlng, wlat


def convert_geom(geom):
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Point":
        return {"type": t, "coordinates": list(gcj02_to_wgs84(*c))}
    if t in ("MultiPoint", "LineString"):
        return {"type": t, "coordinates": [list(gcj02_to_wgs84(*p)) for p in c]}
    if t in ("MultiLineString", "Polygon"):
        return {"type": t, "coordinates": [[list(gcj02_to_wgs84(*p)) for p in ring] for ring in c]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[[list(gcj02_to_wgs84(*p)) for p in ring] for ring in poly]
                                           for poly in c]}
    return geom


OPT_WITH_VALUE = ("--nodata", "--buffer", "--pad")


def buffer_geom(g, meters):
    """按米外扩 GeoJSON 几何（先在经度方向做 cos(lat) 校正，保证近似等距）"""
    geom = shape(g)
    if geom.is_empty:
        return g
    lat = geom.centroid.y
    f = 1.0 / max(math.cos(math.radians(lat)), 1e-6)
    d = meters / 111320.0
    g2 = scale(geom, xfact=f, yfact=1.0, origin=(0, 0))
    g2 = g2.buffer(d)
    g2 = scale(g2, xfact=1.0 / f, yfact=1.0, origin=(0, 0))
    return json.loads(json.dumps(g2.__geo_interface__))


TARGET_CRS = CRS.from_epsg(4326)   # 本 skill 唯一输出坐标系：WGS84 经纬度


def ensure_wgs84(raw_path):
    """输入若不是 EPSG:4326，先重投影到 4326（返回临时文件路径，用完需删）。"""
    with rasterio.open(raw_path) as src:
        if src.crs is None or src.crs == TARGET_CRS:
            return raw_path, None
        src_crs = src.crs
        arr = src.read(1).astype(np.float32)
        src_transform = src.transform
        h, w = arr.shape
        bounds = rasterio.transform.array_bounds(h, w, src_transform)

    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs, TARGET_CRS, w, h, *bounds)
    dst = np.full((dst_h, dst_w), np.nan, dtype=np.float32)
    reproject(arr, dst,
              src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs=TARGET_CRS,
              src_nodata=np.nan, dst_nodata=np.nan,
              resampling=Resampling.bilinear)
    from pathlib import Path
    tmp = Path(raw_path).with_name(Path(raw_path).stem + "_wgs84.tif")
    meta = {"driver": "GTiff", "dtype": "float32", "count": 1, "nodata": np.nan,
            "crs": TARGET_CRS, "height": dst_h, "width": dst_w,
            "transform": dst_transform, "compress": "deflate", "tiled": True}
    with rasterio.open(tmp, "w", **meta) as f:
        f.write(dst[None, ...])
    print(f"输入 {src_crs.to_string()} 已重投影到 EPSG:4326 -> {tmp}")
    return str(tmp), str(tmp)


def clip_to_boundary(raw, boundary, out, do_convert, buffer_m,
                     all_touched, out_nodata, do_fill, pad=3):
    """掩膜裁切并写出，输出坐标系固定 EPSG:4326"""
    gj = json.load(open(boundary, encoding="utf-8"))
    feats = gj["features"] if "features" in gj else [{"geometry": gj}]
    geoms = []
    for f in feats:
        g = f.get("geometry")
        if not g:
            continue
        if do_convert:
            g = convert_geom(g)
        if buffer_m:
            g = buffer_geom(g, buffer_m)
        geoms.append(g)
    if do_convert:
        print("边界已 GCJ-02 -> WGS84")
    if buffer_m:
        print(f"边界外扩 {buffer_m:.0f} m")

    # 读取窗口：**外扩 pad 个像元**再掩膜。
    #
    # 为什么：直接 mask(crop=True) 会把输出栅格裁到边界 bbox，数据范围恰好等于边界范围。
    # 边界最外沿的像元只被部分覆盖，其上采样后的填色到不了矢量边界，视觉上就是
    # 「边界没填满 / 边缘缺一块」。把窗口外扩若干像元，让**真实数据环绕边界一圈**，
    # 渲染时 clip_path 才有完整的数据可裁（用户反馈 #1：数据范围大些）。
    # pad 会被 clamp 在源栅格范围内，不会凭空造出界外区域。
    from rasterio.windows import Window as _Window
    from rasterio.features import geometry_mask as _geomask
    with rasterio.open(raw) as src:
        _shapes = geoms
        _minx = min(shape(s).bounds[0] for s in _shapes)
        _miny = min(shape(s).bounds[1] for s in _shapes)
        _maxx = max(shape(s).bounds[2] for s in _shapes)
        _maxy = max(shape(s).bounds[3] for s in _shapes)
        # 用仿射系数直接反算行列号：不依赖 rasterio.windows.from_bounds
        # （后者要求 bounds 与 transform 严格一致，边界略超栅格或含旋转时会抛
        #  WindowError: Bounds and transform are inconsistent）。
        _a, _b, _c, _d, _e, _f = (src.transform.a, src.transform.b,
                                  src.transform.c, src.transform.d,
                                  src.transform.e, src.transform.f)
        _det = _a * _e - _b * _d
        def _col_of(x, y):
            return ((x - _c) * _e - (y - _f) * _b) / _det
        def _row_of(x, y):
            return ((y - _f) * _a - (x - _c) * _d) / _det
        _cs = [_col_of(x, y) for x in (_minx, _maxx) for y in (_miny, _maxy)]
        _rs = [_row_of(x, y) for x in (_minx, _maxx) for y in (_miny, _maxy)]
        c0 = int(math.floor(min(_cs))) - pad
        c1 = int(math.ceil(max(_cs))) + pad
        r0 = int(math.floor(min(_rs))) - pad
        r1 = int(math.ceil(max(_rs))) + pad
        c0, r0 = max(0, c0), max(0, r0)
        c1, r1 = min(src.width, c1), min(src.height, r1)
        win = _Window(c0, r0, max(1, c1 - c0), max(1, r1 - r0))
        arr_win = src.read(1, window=win)
        out_transform = src.window_transform(win)
        out_meta = src.meta.copy()
        src_tags = src.tags()   # 抓取源 TIF 元数据标签（period/date/source/units 等）

    _keep = _geomask(geoms, out_shape=arr_win.shape, transform=out_transform,
                     invert=True, all_touched=all_touched)
    out_image = np.where(_keep, arr_win, out_nodata)[np.newaxis, ...]
    if pad:
        print(f"窗口外扩 {pad} 像元（数据环绕边界，避免边缘填不满）")

    arr = out_image[0].astype(np.float32)
    arr[(arr <= -32767) | ~np.isfinite(arr)] = out_nodata

    invalid = np.isnan(arr) if np.isnan(out_nodata) else (arr == out_nodata)
    valid = arr[~invalid]
    print(f"有效像元 {valid.size}/{arr.size} ({100 * valid.size / arr.size:.1f}%)")
    if valid.size:
        print(f"变量统计: min={valid.min():.2f} max={valid.max():.2f} mean={valid.mean():.2f}")

    if do_fill and valid.size and invalid.any():
        filled = fillnodata(arr, mask=~invalid, max_search_distance=100, smoothing_iterations=0)
        arr[invalid] = filled[invalid]
        print(f"  补洞 {int(invalid.sum())} 像元")

    lat_c = out_transform.f + arr.shape[0] / 2 * out_transform.e
    dx_m = abs(out_transform.a) * 111320.0 * math.cos(math.radians(lat_c))
    dy_m = abs(out_transform.e) * 110574.0
    out_meta.update({
        "dtype": "float32",
        "nodata": out_nodata,
        "count": 1,
        "crs": TARGET_CRS,
        "height": arr.shape[0],
        "width": arr.shape[1],
        "transform": out_transform,
        "compress": "deflate",
        "tiled": True,
    })
    out_image = arr[None, ...].astype(np.float32)
    with rasterio.open(out, "w", **out_meta) as dst:
        dst.write(out_image)
        if src_tags:
            dst.update_tags(**src_tags)   # 透传周期/日期/数据源/单位到成品 TIF
    print(f"裁切完成: {out}  CRS=EPSG:4326  像元≈{abs(out_transform.a):.7f}°"
          f"（约 {dx_m:.1f} m 东西 × {dy_m:.1f} m 南北）  nodata={out_nodata}")


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
        print("用法: python clip_by_boundary.py raw.tif boundary.json out.tif "
              "[--no-convert] [--strict] [--fill] [--nodata -9999]")
        sys.exit(1)

    raw, boundary, out = pos[0], pos[1], pos[2]
    do_convert = "no-convert" not in flags
    all_touched = "strict" not in flags
    do_fill = "fill" in flags
    nd_opt = opts.get("nodata")
    out_nodata = float(nd_opt) if nd_opt else np.nan
    buffer_m = float(opts["buffer"]) if opts.get("buffer") else 0.0
    pad_opt = opts.get("pad")
    pad = int(pad_opt) if pad_opt else 3

    raw, tmp_wgs84 = ensure_wgs84(raw)
    try:
        clip_to_boundary(raw, boundary, out, do_convert, buffer_m, all_touched,
                         out_nodata, do_fill, pad=pad)
    finally:
        if tmp_wgs84:
            try:
                os.remove(tmp_wgs84)
            except OSError:
                pass


if __name__ == "__main__":
    main()
