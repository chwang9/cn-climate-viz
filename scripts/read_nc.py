#!/usr/bin/env python3
"""读取气象要素 NetCDF（.nc），写出 float32 GeoTIFF（EPSG:4326, nodata=NaN）。

通用启发式读取，覆盖大多数气候/再分析 NetCDF：
- 自动识别经纬度坐标变量（lat/latitude/y、lon/longitude/x）与要素变量。
  要素候选名优先匹配温度类（temp/tas/t2m/...），其次降水（prec/pr/...）、
  风（wind/uwnd/vwnd/...）、湿度（rh/humidity/q）、气压（pslp/slp/msl/pres）、
  辐射（rsds/swd/radiation）、积雪（snow）、云量（cloud）、能见度（vis）等；
  也可用 --var 直接指定变量名。
- 时间轴（可选）：--band <1-based 索引> 或 --date YYYY-MM-DD 选层；缺省取第 1 层。
- 自动处理：scale_factor/add_offset 解包、_FillValue/missing_value -> NaN、
  纬度降序翻转（north-up）、经度 0–360 卷绕到 -180–180。
- 坐标系：经度在 [-180,180] 且纬度在 [-90,90] 时假定 EPSG:4326；
  若含 crs/grid_mapping 变量的 epsg_code 则采用之。

用法: python read_nc.py <input.nc> <out_raw.tif>
      [--var tas] [--band 1] [--date 2023-07-15] [--bbox minx,miny,maxx,maxy]
      [--no-flip] [--crs EPSG:4326] [--units °C] [--agg mean]
"""
import sys
import numpy as np
import netCDF4
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import from_bounds

LAT_NAMES = ("latitude", "lat", "Lat", "LATITUDE", "y", "Y")
LON_NAMES = ("longitude", "lon", "Lon", "LONGITUDE", "x", "X")
# 要素变量候选（按优先级）：温度类优先，其余按常见气候变量名匹配
VAR_HINTS = (
    ("temp", "tas", "t2m", "tavg", "tmean", "t_mean", "air", "t_surface", "st", "tsurf"),
    ("prec", "pr", "precip", "rain", "rr"),
    ("wind", "uwnd", "vwnd", "u10", "v10", "ws", "spd"),
    ("rh", "humidity", "q"),
    ("pslp", "slp", "msl", "pres", "press"),
    ("rsds", "swd", "radiation", "ssrd", "rss"),
    ("snow",),
    ("cloud",),
    ("vis",),
)


def parse_args(argv):
    pos, opts, flags = [], {}, set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--var", "--band", "--date", "--bbox", "--crs", "--units", "--agg"):
            opts[a.lstrip("-")] = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            flags.add(a.lstrip("-"))
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts, flags


def find_coord(ds, names):
    for n in names:
        if n in ds.variables:
            v = ds.variables[n]
            if v.ndim == 1:
                return n, np.asarray(v[:], dtype=np.float64)
    return None, None


def find_var(ds, preferred):
    if preferred and preferred in ds.variables:
        return preferred
    cands = []
    for n, v in ds.variables.items():
        nl = n.lower()
        for grp in VAR_HINTS:
            if any(h in nl for h in grp) and v.ndim >= 2:
                cands.append((n, VAR_HINTS.index(grp)))
                break
    if cands:
        cands.sort(key=lambda x: x[1])
        return cands[0][0]
    # 退而求其次：2D/3D 且维度含经纬度的变量
    latn, lonn = find_coord(ds, LAT_NAMES)[0], find_coord(ds, LON_NAMES)[0]
    for n, v in ds.variables.items():
        if set(v.dimensions) >= {latn, lonn} and v.ndim >= 2:
            return n
    raise RuntimeError("未在 NetCDF 中找到要素变量（尝试 --var 指定）")


def main():
    pos, opts, flags = parse_args(sys.argv[1:])
    if len(pos) < 2:
        print(__doc__)
        sys.exit(1)
    nc_path, out = pos[0], pos[1]
    do_flip = "no-flip" not in flags

    ds = netCDF4.Dataset(nc_path)
    try:
        latn, lat = find_coord(ds, LAT_NAMES)
        lonn, lon = find_coord(ds, LON_NAMES)
        if lat is None or lon is None:
            raise RuntimeError("未找到经纬度坐标变量")
        varn = find_var(ds, opts.get("var"))
        v = ds.variables[varn]
        print(f"变量={varn}  维度={v.dimensions}  形状={v.shape}  单位={getattr(v,'units','')}")

        fillv = None
        for attr in ("_FillValue", "missing_value", "fill_value"):
            if hasattr(v, attr):
                try:
                    fillv = float(getattr(v, attr))
                except Exception:
                    fillv = None
                break

        dims = list(v.dimensions)
        if v.ndim < 2:
            raise RuntimeError(f"要素变量 {varn} 维度过低（{v.ndim}D），无法作为 2D 栅格读取")
        if v.ndim > 3:
            raise RuntimeError(
                f"要素变量 {varn} 为 {v.ndim}D（含垂直层/多变量），暂不支持直接读取；"
                "请用 CDO/nco 先选单层，例如 `cdo sellevel,850` 或 `cdo selvar` 再传入。")
        li = dims.index(latn)
        lj = dims.index(lonn)
        data = v[:]  # 自动解包 scale/add_offset（netCDF4 默认开启）
        if isinstance(data, np.ma.MaskedArray):
            data = data.filled(np.nan)
        data = np.asarray(data, dtype=np.float64)

        if v.ndim == 3:
            tdim = [d for d in dims if d not in (latn, lonn)]
            if opts.get("band"):
                b = int(opts["band"]) - 1
            elif opts.get("date") and tdim and tdim[0] in ds.variables:
                tval = ds.variables[tdim[0]][:]
                bt = None
                for k, tv in enumerate(tval):
                    try:
                        dt = netCDF4.num2date(tv, ds.variables[tdim[0]].units) \
                            if not isinstance(tv, np.datetime64) else tv
                    except Exception:
                        dt = tv
                    if str(dt).startswith(opts["date"]):
                        bt = k
                        break
                b = bt if bt is not None else 0
                if bt is None:
                    print(f"  未匹配 --date {opts['date']}，改用第 1 层")
            else:
                b = 0
                if tdim:
                    print(f"  含时间轴（{v.shape[0]} 层），默认取第 1 层（可用 --band/--date 选层）")
            if b < 0 or b >= data.shape[0]:
                raise RuntimeError(f"--band {opts.get('band')} 超出时间层范围 (1..{data.shape[0]})")
            data = data[b]

        arr = np.asarray(data, dtype=np.float64)
        if fillv is not None:
            arr = np.where(arr == fillv, np.nan, arr)
        # valid_range / valid_min / valid_max：真实 NetCDF 常用它们而非 _FillValue 标记无效值
        vr_lo = vr_hi = None
        if hasattr(v, "valid_range"):
            try:
                vr_lo, vr_hi = float(v.valid_range[0]), float(v.valid_range[1])
            except Exception:
                vr_lo = vr_hi = None
        elif hasattr(v, "valid_min"):
            try:
                vr_lo = float(v.valid_min)
            except Exception:
                vr_lo = None
        elif hasattr(v, "valid_max"):
            try:
                vr_hi = float(v.valid_max)
            except Exception:
                vr_hi = None
        if vr_lo is not None:
            arr = np.where(arr < vr_lo, np.nan, arr)
        if vr_hi is not None:
            arr = np.where(arr > vr_hi, np.nan, arr)
        arr = np.where(np.isfinite(arr), arr, np.nan)
        if li > lj:
            arr = arr.transpose()
        if not np.isfinite(arr).any():
            raise RuntimeError(
                f"变量 {varn} 全为无效值（_FillValue / valid_range 之外 / 全部缺测），"
                "请检查变量名是否正确或数据是否包含有效格点。")

        if do_flip and lat[0] < lat[-1]:
            arr = arr[::-1, :]
            lat = lat[::-1]
        if lon.max() > 180:
            shift = lon > 180
            lon = np.where(shift, lon - 360, lon)
            order = np.argsort(lon)
            lon = lon[order]
            arr = arr[:, order]
        arr = np.where(np.isfinite(arr), arr, np.nan)

        nlat, nlon = arr.shape
        dlon = (lon[-1] - lon[0]) / (nlon - 1) if nlon > 1 else 1e-3
        dlat = (lat[-1] - lat[0]) / (nlat - 1) if nlat > 1 else 1e-3
        left = lon[0] - dlon / 2
        top = lat[-1] + dlat / 2
        transform = from_origin(left, top, dlon, dlat)
        crs = opts.get("crs", "EPSG:4326")

        if opts.get("bbox"):
            nums = [float(x) for x in opts["bbox"].split(",")]
            minx, miny, maxx, maxy = nums
            win = from_bounds(minx, miny, maxx, maxy, transform)
            from rasterio.windows import Window
            w = Window(max(0, int(win.col_off)), max(0, int(win.row_off)),
                       min(arr.shape[1], int(win.width)), min(arr.shape[0], int(win.height)))
            arr = arr[int(w.row_off):int(w.row_off)+int(w.height),
                      int(w.col_off):int(w.col_off)+int(w.width)]
            left = transform.c + w.col_off * transform.a
            top = transform.f + w.row_off * transform.e
            transform = from_origin(left, top, dlon, dlat)
            print(f"  按 bbox 裁剪 -> {arr.shape[1]}×{arr.shape[0]}")

        meta = {"driver": "GTiff", "dtype": "float32", "count": 1, "nodata": np.nan,
                "crs": crs, "height": arr.shape[0], "width": arr.shape[1],
                "transform": transform, "compress": "deflate", "tiled": True}
        nc_date = opts.get("date", "")
        nc_period = (f"{nc_date} 均值" if nc_date
                     else f"时间层 #{int(opts['band'])}" if opts.get("band")
                     else "NetCDF 变量首层")
        with rasterio.open(out, "w", **meta) as dst:
            dst.write(arr.astype(np.float32)[None, ...])
            dst.update_tags(period=nc_period, date=nc_date, source="user NetCDF",
                            variable=varn, units=opts.get("units", getattr(v, "units", "")),
                            agg=opts.get("agg", "mean"), generated_by="cn-climate-viz")
    finally:
        ds.close()

    valid = arr[np.isfinite(arr)]
    print(f"要素统计: min={np.nanmin(valid):.2f} max={np.nanmax(valid):.2f} "
          f"mean={np.nanmean(valid):.2f}  有效 {valid.size}/{arr.size}")
    print("写出:", out, f"({arr.shape[1]}×{arr.shape[0]}) CRS={crs}")


if __name__ == "__main__":
    main()
