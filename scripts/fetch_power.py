#!/usr/bin/env python3
"""按省/市边界下载任意气象要素栅格，写出中间 float32 GeoTIFF（EPSG:4326, nodata=NaN）。

数据源：NASA POWER（https://power.larc.nasa.gov，免 Key，GET 单点接口，底层 MERRA-2）。
- 变量由调用方经 --var 传入 **POWER 参数名**（如 T2M / WS10M / PRECTOT / RH2M / PS /
  ALLSKY_SFC_SW_DWN / T2M_MAX / WS10M_MAX），聚合方式 --agg mean|sum|min|max。
- 周期（支持时间段）：
    * --date YYYY-MM-DD：单日；
    * --month M [--year Y]：该月；
    * --year Y：该年（01-01 ~ 12-31 整段聚合）；
    * --start S --end E：任意时间段。
  POWER 单点接口返回该段逐日值，本模块在本地按 agg 聚合成单层栅格。
- 网格：边界 WGS84 bbox 内铺经纬度格点逐点采样。MERRA-2 原生 0.5°，本模块按 0.1° 默认
  密集采样以获得更平滑的市级尺度结果；年/大范围模式自动限制格点数以控请求数。
- 网络纪律：沿用本机网关实测经验——5xx 短等、429 长等（尊重 Retry-After）、失败重试退避，
  单点失败不致命（整体用最近邻插值补齐空洞）。

用法: python fetch_power.py <boundary.json> <out_raw.tif>
      [--var WS10M] [--agg mean]
      [--year 2023] [--month 7] [--date 2023-07-15]
      [--start 2023-06-01] [--end 2023-08-31]
      [--res 0.1] [--buffer 0.1] [--workers 6] [--factor 1.0] [--units m/s]
"""
import sys
import time
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import numpy as np
import rasterio
from rasterio.transform import from_origin

from geo_utils import load_boundary_wgs84, TARGET_CRS, fill_nearest_numpy
from fetch_climate import build_grid

API = "https://power.larc.nasa.gov/api/temporal/daily/point"
MISSING = -999.0
OPT_WITH_VALUE = ("--var", "--agg", "--year", "--month", "--date",
                  "--start", "--end", "--res", "--buffer", "--workers",
                  "--factor", "--units")


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


def _ymd(s):
    """'2023-06-01' -> '20230601'；已是纯数字则原样返回。"""
    return s.replace("-", "")


def period_to_ymd(kind, year, month, date, start, end):
    """返回 (start_ymd, end_ymd) 用于 POWER 接口。"""
    if kind == "date":
        return _ymd(date), _ymd(date)
    if kind == "month":
        last = calendar.monthrange(year, month)[1]
        return f"{year:04d}{month:02d}01", f"{year:04d}{month:02d}{last:02d}"
    if kind == "annual":
        return f"{year:04d}0101", f"{year:04d}1231"
    # range
    return _ymd(start), _ymd(end)


def build_url(param, lat, lon, start_ymd, end_ymd):
    return (f"{API}?parameters={param}&start={start_ymd}&end={end_ymd}"
            f"&latitude={lat:.4f}&longitude={lon:.4f}&format=CSV&community=RE")


def parse_daily(text, param):
    """从 POWER CSV 提取该参量逐日值列表（剔除 -999 缺测）。失败返回 []。"""
    lines = text.splitlines()
    head_i = None
    for i, ln in enumerate(lines):
        cols = ln.split(",")
        # 表头形如 YEAR,MO,DY,WS10M（param 可能带单位后缀如 WS10M 或 WS10M_MAX）
        if cols and cols[0].strip() == "YEAR" and len(cols) >= 4:
            head_i = i
            # 定位参数列（兼容 WS10M / WS10M_MAX / ALLSKY_SFC_SW_DWN 等）
            try:
                vi = next(j for j, h in enumerate(cols) if h.strip() == param)
            except StopIteration:
                # 参数名带单位后缀（如 WS10M_km/h），用前缀匹配
                try:
                    vi = next(j for j, h in enumerate(cols) if h.strip().startswith(param))
                except StopIteration:
                    return []
            break
    if head_i is None:
        return []
    vals = []
    for ln in lines[head_i + 1:]:
        ln = ln.strip()
        if not ln or ln.startswith("–") or ln.startswith("-"):
            # 空行或落款行（'– ...'）截止
            if not ln:
                continue
            if ln[0] in "–-":
                break
        parts = ln.split(",")
        if len(parts) <= vi:
            continue
        try:
            v = float(parts[vi])
        except (ValueError, IndexError):
            continue
        if v <= MISSING + 1:  # 缺测 -999
            continue
        vals.append(v)
    return vals


def fetch_point(param, lat, lon, start_ymd, end_ymd, agg, factor, retries=5):
    """单点取数并按 agg 聚合；失败返回 None。"""
    url = build_url(param, lat, lon, start_ymd, end_ymd)
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=(20, 90))
            if r.status_code == 200:
                vals = parse_daily(r.text, param)
                if not vals:
                    return None
                arr = np.asarray(vals, dtype=np.float64) * float(factor)
                if agg == "sum":
                    return float(arr.sum())
                if agg == "min":
                    return float(arr.min())
                if agg == "max":
                    return float(arr.max())
                return float(arr.mean())
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait = 10 + attempt * 10
                if ra:
                    try:
                        wait = max(wait, int(ra))
                    except (TypeError, ValueError):
                        pass
                print(f"  429 限流，等待 {wait}s 后重试 (点 {lat:.2f},{lon:.2f})")
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                r.raise_for_status()
            # 4xx 客户端错误：该点直接放弃（不重试）
            print(f"  POWER 返回 {r.status_code}（点 {lat:.2f},{lon:.2f}），跳过")
            return None
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                print(f"  请求异常 {type(e).__name__}（点 {lat:.2f},{lon:.2f}），放弃")
                return None
            time.sleep(3 + attempt * 2)
    return None


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 2:
        print(__doc__)
        sys.exit(1)
    boundary, out = pos[0], pos[1]
    param = opts.get("var", "T2M")
    agg = (opts.get("agg") or "mean").lower()
    year = int(opts.get("year", 2023)) if opts.get("year") else None
    month = int(opts["month"]) if opts.get("month") else None
    date = opts.get("date")
    start = opts.get("start")
    end = opts.get("end")
    res = float(opts["res"]) if opts.get("res") else None
    pad = float(opts["buffer"]) if opts.get("buffer") else 0.0
    workers = int(opts.get("workers", 6))
    factor = float(opts.get("factor", 1.0))
    units = opts.get("units", "")

    # 周期判定（与 fetch_climate 一致）
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

    cap = 300 if kind in ("annual", "range") else 2500
    _, bounds, _ = load_boundary_wgs84(boundary, do_convert=True)
    lats, lons, nrow, ncol, dx, dy, ext = build_grid(
        bounds, cap, min_spacing=0.04, res_override=res, pad_deg=pad, pad_cells=3)
    minx, miny, maxx, maxy = ext
    start_ymd, end_ymd = period_to_ymd(kind, year, month, date, start, end)

    period_desc = (f"{start}~{end}" if kind == "range"
                   else date if kind == "date"
                   else f"{year}年{month}月" if kind == "month"
                   else f"{year}年")
    print(f"数据源=NASA POWER  参数={param} 聚合={agg} 周期={kind}({period_desc})"
          f"  网格={ncol}×{nrow}  间距≈{dx:.3f}°×{dy:.3f}°  "
          f"bbox=({minx:.3f},{miny:.3f},{maxx:.3f},{maxy:.3f})")
    print(f"  注：MERRA-2 原生 0.5°，{dx:.3f}° 网格为本地密集采样后聚合")

    arr = np.full((nrow, ncol), np.nan, dtype=np.float32)
    jobs = [(ri, ci, lats[ri], lons[ci]) for ri in range(nrow) for ci in range(ncol)]
    done = fail = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        fut = {ex.submit(fetch_point, param, lat, lon, start_ymd, end_ymd, agg, factor): (ri, ci)
               for ri, ci, lat, lon in jobs}
        for f in as_completed(fut):
            ri, ci = fut[f]
            v = f.result()
            if v is not None and np.isfinite(v):
                arr[ri, ci] = v
                done += 1
            else:
                fail += 1
            if (done + fail) % 50 == 0:
                print(f"  进度 {done+fail}/{len(jobs)}  成功 {done} 失败 {fail}")

    if not np.isfinite(arr).any():
        raise RuntimeError(
            f"全部 {len(jobs)} 个采样点均未返回有效数据。POWER 可能限流或不可达，"
            "请稍后重试，或改用 --source open-meteo / --nc。")

    # 网关偶发失败会让个别格点缺失；最近邻补齐，避免成品出现大块空洞
    if not np.isfinite(arr).all():
        invalid = ~np.isfinite(arr)
        n_bad = int(invalid.sum())
        arr, _iters = fill_nearest_numpy(arr, invalid)
        print(f"  缺失 {n_bad} 点，已用最近邻插值补齐（{_iters} 轮）")

    transform = from_origin(minx, maxy, dx, dy)  # e=-dy（北朝上，正确！）
    meta = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "nodata": np.nan,
        "crs": TARGET_CRS, "height": nrow, "width": ncol,
        "transform": transform, "compress": "deflate", "tiled": True,
    }
    agg_suffix = {"sum": " 累计", "mean": " 平均", "min": " 最小", "max": " 最大"}.get(agg, "")
    period_tag = (f"{start}~{end}{agg_suffix}" if kind == "range"
                  else f"{date} 均值" if kind == "date"
                  else f"{year}年{month}月{agg_suffix}" if kind == "month"
                  else f"{year}年{agg_suffix}")
    with rasterio.open(out, "w", **meta) as dst:
        dst.write(arr.astype(np.float32)[None, ...])
        dst.update_tags(period=period_tag, start=start_ymd, end=end_ymd, date=(_ymd(date) if date else ""),
                        source="NASA POWER (power.larc.nasa.gov) / MERRA-2",
                        units=units, variable=param, agg=agg,
                        generated_by="cn-climate-viz")
    valid = arr[np.isfinite(arr)]
    if valid.size:
        print(f"{param} 统计: min={valid.min():.2f} max={valid.max():.2f} "
              f"mean={valid.mean():.2f}  有效像元 {valid.size}/{arr.size}")
    print("原始栅格:", out, f"({ncol}×{nrow})  源=NASA POWER/MERRA-2")


if __name__ == "__main__":
    main()
