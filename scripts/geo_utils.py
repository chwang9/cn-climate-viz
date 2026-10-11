#!/usr/bin/env python3
"""气象要素可视化 skill 的共用工具：GCJ-02<->WGS84 转换、边界读取。

- DataV 行政边界为 GCJ-02，气象/影像要素数据均为 WGS84，裁切前必须反解到 WGS84。
- 科研级配色（ColorBrewer 各调色板）集中在 variables.py，本模块只负责坐标与边界。
"""
import json
import math
from pathlib import Path

import numpy as np
from shapely.geometry import shape
from shapely.affinity import scale
from shapely.ops import unary_union

TARGET_CRS = "EPSG:4326"  # 本 skill 唯一输出坐标系：WGS84 经纬度

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


def wgs84_to_gcj02(lng, lat):
    """WGS84 -> GCJ-02（高德/腾讯地图坐标系），中国境外原样返回。"""
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _tlat(lng - 105.0, lat - 35.0)
    dlng = _tlng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - GCJ_EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (GCJ_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lng + dlng, lat + dlat


def convert_geom_to_gcj02(geom):
    """把 GeoJSON 几何的 WGS84 坐标转为 GCJ-02（递归处理所有几何类型），用于对齐高德底图。"""
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Point":
        return {"type": t, "coordinates": list(wgs84_to_gcj02(*c))}
    if t in ("MultiPoint", "LineString"):
        return {"type": t, "coordinates": [list(wgs84_to_gcj02(*p)) for p in c]}
    if t in ("MultiLineString", "Polygon"):
        return {"type": t, "coordinates": [[list(wgs84_to_gcj02(*p)) for p in ring] for ring in c]}
    if t == "MultiPolygon":
        return {"type": t, "coordinates": [[[list(wgs84_to_gcj02(*p)) for p in ring] for ring in poly]
                                           for poly in c]}
    return geom


def convert_geom(geom):
    """把 GeoJSON 几何的 GCJ-02 坐标反解为 WGS84（递归处理所有几何类型）"""
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


def load_boundary_wgs84(path, do_convert=True):
    """读取 DataV 边界 GeoJSON，返回 (wgs84_features, bounds, union_polygon)。

    bounds = (minx, miny, maxx, maxy)，union_polygon 为合并后的 WGS84 多边形。
    do_convert=False 时跳过 GCJ-02->WGS84（边界已是 WGS84）。
    """
    gj = json.loads(Path(path).read_text(encoding="utf-8"))
    feats = gj["features"] if "features" in gj else [{"geometry": gj, "properties": {}}]
    geoms = []
    for f in feats:
        g = f.get("geometry")
        if not g:
            continue
        if do_convert:
            g = convert_geom(g)
        geoms.append(shape(g))
    if not geoms:
        raise RuntimeError("边界无有效几何")
    union = geoms[0] if len(geoms) == 1 else unary_union(geoms)
    if union.geom_type.startswith("Multi"):
        union = max(union.geoms, key=lambda p: p.area)
    bounds = union.bounds  # (minx, miny, maxx, maxy)
    return feats, bounds, union


def buffer_geom(g, meters):
    """按米外扩 GeoJSON 几何（经度方向做 cos(lat) 校正，保证近似等距）"""
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


# ---------- 气温科研配色（ColorBrewer RdYlBu 反转：冷蓝 -> 暖红） ----------
_RdYlBu_r = [
    (49, 53, 149), (69, 117, 178), (116, 173, 209), (171, 217, 233),
    (224, 243, 248), (255, 255, 191), (254, 224, 144), (253, 174, 97),
    (252, 141, 89), (215, 48, 39), (165, 0, 38),
]
# 同款调色板的十六进制，用于 HTML 图例渐变
RdYlBu_r_HEX = ["#313695", "#4575b4", "#74add1", "#abd9e9", "#e0f3f8",
                "#ffffbf", "#fee090", "#fdae61", "#f46d43", "#d73027", "#a50026"]


def nice_range(vmin, vmax, n=11):
    """把 [vmin, vmax] 扩展成对称的、漂亮的整数/半整数刻度区间"""
    if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
        return -30.0, 30.0
    lo = math.floor(vmin)
    hi = math.ceil(vmax)
    # 向 0 对齐下界、向上对齐上界，保证冷暖对称感
    lo = min(lo, -abs(hi)) if lo < 0 else lo
    return float(lo), float(hi)


def colorize_temperature(arr, vmin, vmax):
    """把气温数组按 RdYlBu_r 上色，返回 uint8 RGB（与 arr 同形状 + (3,)）。

    nodata（NaN）保持 (0,0,0)；调用方再据掩膜转透明。
    """
    a = np.asarray(arr, dtype=np.float32)
    out = np.zeros(a.shape + (3,), dtype=np.float32)
    invalid = ~np.isfinite(a)
    t = np.where(np.isfinite(a), np.clip((a - vmin) / (vmax - vmin + 1e-9), 0.0, 1.0), 0.0)
    stops = len(_RdYlBu_r)
    pos = t * (stops - 1)
    i0 = np.floor(pos).astype(int)
    i0 = np.clip(i0, 0, stops - 2)
    frac = pos - i0
    for k in range(3):
        c0 = np.array([s[k] for s in _RdYlBu_r], dtype=np.float32)
        c1 = np.array([_RdYlBu_r[min(j + 1, stops - 1)][k] for j in range(stops)], dtype=np.float32)
        out[..., k] = c0[i0] + (c1[i0] - c0[i0]) * frac
    out = np.clip(out, 0, 255).astype(np.uint8)
    out[invalid] = (0, 0, 0)
    return out


def fill_nearest_numpy(arr, invalid=None, max_iter=None):
    """用**最近邻**填补无效格点（纯 numpy，替代 scipy.ndimage.distance_transform_edt）。

    做法：迭代 4 邻域膨胀 —— 每轮把紧邻有效值的一圈无效格点填上，
    轮数 = 到最近有效值的最大距离。气象格点通常 <200x200，足够快，
    且免去 scipy 的 1067 ms 导入开销（实测见 make_figure.py 头部注释）。

    参数：
      arr      : 数值数组
      invalid  : 无效掩膜（默认 ~np.isfinite(arr)）
      max_iter : 迭代上限（默认 行数+列数，即最坏情形）
    返回 (填补后的数组, 迭代轮数)。
    """
    a = np.asarray(arr, dtype=np.float64)
    if invalid is None:
        invalid = ~np.isfinite(a)
    invalid = np.asarray(invalid, dtype=bool)
    out = np.where(invalid, 0.0, a).astype(np.float64)
    src = ~invalid
    todo = invalid.copy()
    n = 0
    lim = int(max_iter) if max_iter else int(a.shape[0] + a.shape[1])
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
