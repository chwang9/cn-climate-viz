#!/usr/bin/env python3
"""按省/市边界下载任意气象要素栅格，写出中间 float32 GeoTIFF（EPSG:4326, nodata=NaN）。

数据源：Open-Meteo Archive API（https://archive-api.open-meteo.com，免 Key，GET 单请求）。
- 变量由调用方经 --var 传入（如 temperature_2m_mean / precipitation_sum / wind_speed_10m_mean），
  聚合方式 --agg mean|sum|min|max 取决于要素（气温取均值、降水取求和等）。
- 周期（支持时间段）：
    * --date YYYY-MM-DD：单日；
    * --month M [--year Y]：该月；
    * --year Y：该年（12 个月各自分块 GET 后按 agg 累计）；
    * --start S --end E：任意时间段（按自然月分块 GET 后按 agg 累计）。
- 网格：边界 WGS84 bbox 内铺经纬度格点。Open-Meteo 多点接口单请求 URL 有长度上限，
  故每批 CHUNK 个点并发 GET（线程池），按 location_id 映射回全局格点索引。
  年/大范围模式自动限制格点数以控制请求数；月/日模式允许更密。

用法: python fetch_climate.py <boundary.json> <out_raw.tif>
      [--var temperature_2m_mean] [--agg mean]
      [--year 2023] [--month 7] [--date 2023-07-15]
      [--start 2023-06-01] [--end 2023-08-31]
      [--res 0.1] [--buffer 0.1] [--no-convert] [--workers 8]
"""
import sys
import math
import csv
import time
import calendar
from concurrent.futures import ThreadPoolExecutor
import requests
import numpy as np
import rasterio
from rasterio.transform import from_origin

from geo_utils import load_boundary_wgs84, TARGET_CRS, fill_nearest_numpy

API = "https://archive-api.open-meteo.com/v1/archive"
# 每批 GET 格点数。实测（本机 Open-Meteo 免费档，顺序 0 间隔 20 次/档）：
#   CHUNK 10→0% 429、20/30→5%、40→35%、60→100%。即 429 为硬性单请求体积限流，
#   且 9.5min 全程重试仍 100% 429 -> 重试同大块必再 429，长等纯浪费墙钟。
# 取 20：429 率≈5%（失败批交最近邻插值补齐，数据仍≈95% 真实），请求数适中墙钟可控；
# 上游限流放松时可按需调大。并发由 workers 控制。
CHUNK = 20
OPT_WITH_VALUE = ("--var", "--agg", "--year", "--month", "--date",
                  "--start", "--end", "--res", "--buffer", "--workers")


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


def month_range(start_s, end_s):
    """把 [start,end] 切成若干 (seg_start, seg_end) 自然月闭区间字符串列表。"""
    sy, sm, sd = (int(x) for x in start_s.split("-"))
    ey, em, ed = (int(x) for x in end_s.split("-"))
    segs = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        first = f"{y:04d}-{m:02d}-01"
        last = f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
        # 首段裁剪到 start，末段裁剪到 end
        s = first if (y, m) != (sy, sm) else start_s
        e = last if (y, m) != (ey, em) else end_s
        segs.append((s, e))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return segs


def build_grid(bounds, cap_points, min_spacing, res_override, pad_deg, pad_cells=0):
    """按 bbox 构建规则采样网格。

    pad_deg     : 显式外扩度数（用户 --buffer）。
    pad_cells   : **按像元外扩**（>=1 时启用）。取数范围必须比行政边界大出若干像元，
                  否则边界最外沿的像元只被部分覆盖——上采样后填色到不了矢量边界，
                  视觉上就是「边界没填满」。外扩量 = pad_cells × 像元间距，
                  因像元间距取决于外扩后的跨度，这里用一次迭代收敛。
    """
    minx, miny, maxx, maxy = bounds
    if pad_deg:
        minx -= pad_deg; miny -= pad_deg; maxx += pad_deg; maxy += pad_deg

    def _spacing(span_lon, span_lat):
        if res_override:
            return float(res_override), float(res_override)
        ratio = span_lon / span_lat if span_lat > 0 else 1.0
        ncol = max(8, min(140, int(round(math.sqrt(cap_points * ratio)))))
        nrow = max(8, min(140, int(round(cap_points / ncol))))
        dx, dy = span_lon / ncol, span_lat / nrow
        if dx < min_spacing or dy < min_spacing:
            dx = dy = min_spacing
        return dx, dy

    # 先按未外扩跨度估像元间距，再据此外扩 pad_cells 个像元（迭代一次即足够）
    if pad_cells and pad_cells > 0:
        dx0, dy0 = _spacing(maxx - minx, maxy - miny)
        minx -= pad_cells * dx0; maxx += pad_cells * dx0
        miny -= pad_cells * dy0; maxy += pad_cells * dy0

    lon_span, lat_span = maxx - minx, maxy - miny
    dx, dy = _spacing(lon_span, lat_span)
    ncol = max(1, int(round(lon_span / dx)))
    nrow = max(1, int(round(lat_span / dy)))
    dx, dy = lon_span / ncol, lat_span / nrow
    lons = [minx + (i + 0.5) * dx for i in range(ncol)]
    lats = [maxy - (j + 0.5) * dy for j in range(nrow)]
    return lats, lons, nrow, ncol, dx, dy, (minx, miny, maxx, maxy)


def _get_csv(url, retries=3, timeout=(10, 20)):
    """GET 单批 CSV，按状态码分策重试（测量优先：本机成功上限≈8.3s → read 超时 20s）。

    网络纪律（沿用本机网关实测经验）：
    - 200           : 返回文本
    - 429 限流      : 长等——尊重响应头 Retry-After，否则指数退避封顶 60s（限流必须等）
    - 5xx(含网关504): 短等 1s 后重试——网关偶发拦截，立刻重试常能命中（"5xx 短等"）
    - 其他 4xx      : 客户端错误，不重试直接报错（快速失败，避免白耗 3 次重试）
    - 异常/超时     : 短等 1s 后重试（"失败重试的固定 sleep 是隐性大户" → 压到 1s）
    """
    for i in range(retries):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait = 0
                if ra:
                    try:
                        wait = int(ra)
                    except (TypeError, ValueError):
                        wait = 0
                if wait > 0:
                    # 服务端明确告知冷却：礼貌等待后重试（"429 才长等"）
                    print(f"  429 限流(Retry-After={wait}s)，等待后重试 {i + 1}/{retries}")
                    time.sleep(wait)
                    continue
                # 无 Retry-After：本机实测 Open-Meteo 免费档对大块 429 为硬性体积限流，
                # 9.5min 全程重试仍 100% 429 -> 长等重试纯浪费墙钟。快速失败，交最近邻插值补齐。
                print(f"  429 限流(无 Retry-After)，放弃本批重试 {i + 1}/{retries}")
                raise RuntimeError("Open-Meteo 429 限流(本批放弃，转插值补齐)")
            if 500 <= r.status_code < 600:
                if i < retries - 1:
                    print(f"  {r.status_code} 服务端错误，短等 1s 后重试 {i + 1}/{retries}")
                    time.sleep(1)
                    continue
                r.raise_for_status()
            # 4xx 客户端错误：不重试，直接抛错
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            if i == retries - 1:
                raise
            print(f"  请求异常 {type(e).__name__}，短等 1s 后重试 {i + 1}/{retries}")
            time.sleep(1)
    raise RuntimeError("Open-Meteo 请求失败")


def _parse_chunk(text, var, base, npts):
    """解析一个分块 GET 的 CSV，返回 {global_idx: [每日值...]}（NaN 已剔除）。

    Open-Meteo CSV 含两段表头：先是 location 元数据表头，再是
    `location_id,time,<var> ...` 数据表头。这里定位含 'time' 的数据表头，
    并按 location_id 聚合该批所有日期的数值。
    """
    rows = list(csv.reader(text.splitlines()))
    head_i = None
    for i, r in enumerate(rows):
        if r and r[0] == "location_id" and any(h.startswith(var) for h in r):
            head_i = i
            break
    if head_i is None:
        return {}
    head = rows[head_i]
    try:
        li = head.index("location_id")
        vi = next(i for i, h in enumerate(head) if h.startswith(var))
    except (ValueError, StopIteration):
        return {}
    out = {}
    for r in rows[head_i + 1:]:
        if len(r) <= max(li, vi):
            continue
        try:
            loc = int(r[li]) + base
            val = float(r[vi])
        except (ValueError, IndexError):
            continue
        if loc < 0 or loc >= npts or not np.isfinite(val):
            continue
        out.setdefault(loc, []).append(val)
    return out


def fetch_period(lats, lons, kind, year, month, date, start, end,
                 var, agg, unit_params="", workers=8):
    nrow, ncol = len(lats), len(lons)
    flat_lat = [lats[j] for j in range(nrow) for _ in range(ncol)]
    flat_lon = [lons[i] for _ in range(nrow) for i in range(ncol)]
    npts = nrow * ncol

    # 生成 (seg_start, seg_end) 时间段列表
    if kind == "date":
        segs = [(date, date)]
    elif kind == "month":
        segs = [(f"{year}-{month:02d}-01",
                 f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}")]
    elif kind == "annual":
        segs = [(f"{year}-{m:02d}-01",
                 f"{year}-{m:02d}-{calendar.monthrange(year, m)[1]:02d}")
                for m in range(1, 13)]
    else:  # range
        segs = month_range(start, end)

    tasks = []
    for s, e in segs:
        for base in range(0, npts, CHUNK):
            be = min(base + CHUNK, npts)
            lat_s = ",".join(f"{v:.4f}" for v in flat_lat[base:be])
            lon_s = ",".join(f"{v:.4f}" for v in flat_lon[base:be])
            url = (f"{API}?latitude={lat_s}&longitude={lon_s}"
                   f"&start_date={s}&end_date={e}&daily={var}"
                   f"&timezone=Asia%2FShanghai&format=csv{unit_params}")
            tasks.append((url, base, s, e))

    # 按聚合类型累计：mean/sum 用 acc_sum/acc_cnt；min/max 另记极值
    acc_sum = np.zeros(npts, dtype=np.float64)
    acc_cnt = np.zeros(npts, dtype=np.int32)
    acc_min = np.full(npts, np.inf, dtype=np.float64)
    acc_max = np.full(npts, -np.inf, dtype=np.float64)

    def worker(t):
        url, base, s, e = t
        try:
            text = _get_csv(url)
        except Exception as ex:
            print(f"  GET 失败 {s}~{e} base={base}: {ex}")
            return t, {}
        return t, _parse_chunk(text, var, base, npts)

    failed = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for t, res in ex.map(worker, tasks):
            if res:
                for loc, vals in res.items():
                    for v in vals:
                        acc_sum[loc] += v
                        acc_cnt[loc] += 1
                        if v < acc_min[loc]:
                            acc_min[loc] = v
                        if v > acc_max[loc]:
                            acc_max[loc] = v
                done += 1
            else:
                failed.append(t)
    print(f"  {done}/{len(tasks)} 批 GET 完成, {len(failed)} 批失败（空缺由最近邻补齐）")

    if agg == "sum":
        out = np.where(acc_cnt > 0, acc_sum, np.nan)
    elif agg == "min":
        out = np.where(acc_cnt > 0, acc_min, np.nan)
    elif agg == "max":
        out = np.where(acc_cnt > 0, acc_max, np.nan)
    else:  # mean
        out = np.where(acc_cnt > 0, acc_sum / np.clip(acc_cnt, 1, None), np.nan)
    return out.reshape(nrow, ncol), len(tasks), len(failed)


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 2:
        print(__doc__)
        sys.exit(1)
    boundary, out = pos[0], pos[1]
    var = opts.get("var", "temperature_2m_mean")
    agg = (opts.get("agg") or "mean").lower()
    year = int(opts.get("year", 2023)) if opts.get("year") else None
    month = int(opts["month"]) if opts.get("month") else None
    date = opts.get("date")
    start = opts.get("start")
    end = opts.get("end")
    res = float(opts["res"]) if opts.get("res") else None
    pad = float(opts["buffer"]) if opts.get("buffer") else 0.0
    workers = int(opts.get("workers", 4))
    do_convert = "no-convert" not in flags
    unit_params = opts.get("unit_params", "")  # 由编排器注入（如 &wind_speed_unit=m/s）

    # 周期判定
    if start and end:
        kind = "range"
    elif date:
        kind = "date"
    elif month:
        kind = "month"
        if year is None:
            year = 2023
    elif year:
        kind = "annual"
    else:
        kind = "annual"
        year = 2023

    # 密度上限：年/范围模式较粗，月/日模式可密
    cap = 250 if kind in ("annual", "range") else 3000

    _, bounds, _ = load_boundary_wgs84(boundary, do_convert=do_convert)
    lats, lons, nrow, ncol, dx, dy, ext = build_grid(
        bounds, cap, min_spacing=0.04, res_override=res, pad_deg=pad, pad_cells=3)

    period_desc = (f"{start} ~ {end}" if kind == "range"
                   else date if kind == "date"
                   else f"{year}年{month}月" if kind == "month"
                   else f"{year}年")
    print(f"变量={var} 聚合={agg} 周期={kind}({period_desc})"
          f"  网格={ncol}×{nrow}  间距≈{dx:.3f}°×{dy:.3f}°  "
          f"bbox=({ext[0]:.3f},{ext[1]:.3f},{ext[2]:.3f},{ext[3]:.3f})")

    arr, n_tasks, n_failed = fetch_period(lats, lons, kind, year, month, date, start, end,
                                          var, agg, unit_params=unit_params, workers=workers)

    if not np.isfinite(arr).any():
        raise RuntimeError(
            f"全部 {n_tasks} 批请求（{n_failed} 批失败）均未返回有效数据，栅格全为 NaN。"
            "请检查网络（网关/代理是否拦截 GET 或当前不可达）后稍后重试。")

    # 网关偶发 504 会让个别格点缺失；用最近邻填充空洞，避免成品出现大块空白
    if not np.isfinite(arr).all():
        invalid = ~np.isfinite(arr)
        n_bad = int(invalid.sum())
        arr, _iters = fill_nearest_numpy(arr, invalid)
        print(f"  缺失格点 {n_bad} 个，已用最近邻插值补齐（{_iters} 轮）")

    minx, miny, maxx, maxy = ext
    transform = from_origin(minx, maxy, dx, dy)
    meta = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "nodata": np.nan,
        "crs": TARGET_CRS, "height": nrow, "width": ncol,
        "transform": transform, "compress": "deflate", "tiled": True,
    }
    agg_suffix = {"sum": " 累计", "mean": " 平均", "min": " 最小", "max": " 最大"}.get(agg, "")
    period_tag = (f"{start} ~ {end}{agg_suffix}" if kind == "range"
                  else f"{date} 均值" if kind == "date"
                  else f"{year}年{month}月{agg_suffix}" if kind == "month"
                  else f"{year}年{agg_suffix}")
    with rasterio.open(out, "w", **meta) as dst:
        dst.write(arr.astype(np.float32)[None, ...])
        dst.update_tags(period=period_tag, start=(start or ""), end=(end or ""),
                        date=(date or ""), source="Open-Meteo Archive (archive-api.open-meteo.com)",
                        units=opts.get("units", ""), variable=var, agg=agg,
                        generated_by="cn-climate-viz")
    valid = arr[np.isfinite(arr)]
    if valid.size:
        print(f"{var} 统计: min={valid.min():.2f} max={valid.max():.2f} "
              f"mean={valid.mean():.2f}  有效像元 {valid.size}/{arr.size}")
    print("原始栅格:", out, f"({arr.shape[1]}×{arr.shape[0]})")


if __name__ == "__main__":
    main()
