#!/usr/bin/env python3
"""一条命令跑完: 省/市/区县名 -> 任意气象要素栅格 -> 裁切 GeoTIFF -> HTML 预览 -> 科研级静态出图。

用法:
  python cn_climate.py <区划名/adcode> [输出目录] [选项]
  python cn_climate.py 合肥市 out --preset precipitation --start 2023-06-01 --end 2023-08-31
  python cn_climate.py 安徽省 out --preset temperature --year 2023
  python cn_climate.py 合肥市 out --preset wind --month 6 --year 2026 --source nasa-power
  python cn_climate.py 合肥市 out --nc my.nc --var tas            # 直接读自备 NetCDF
  python cn_climate.py 合肥市 out --var temperature_2m_max        # 直接传 Open-Meteo 变量名

输入坐标系固定 WGS84（EPSG:4326）。输出: <名>_<preset>.tif(成品) / <名>_<preset>.html(交互预览)
  / <名>_<preset>_figure.png(科研级静态出图, 默认 300dpi) / <名>_<preset>_preview.png(HTML 叠加层)
  / <名>.json(边界) / <名>_raw.tif(中间文件，可删)。

数据源三选一：--source open-meteo（默认，免 Key）/ --source nasa-power（NASA POWER MERRA-2，免 Key，
Open-Meteo 限流时的首选备选）/ --nc（自备 NetCDF）。同名区划（如朝阳区）用 --parent 消歧。

支持的预设（--preset）见 scripts/variables.py 的 VARIABLES：
  temperature / temperature_max / temperature_min / precipitation / snowfall /
  wind / wind_max / humidity / pressure / radiation / cloud / visibility
"""
import sys
import subprocess
from pathlib import Path

HERE = Path(__file__).parent

# 把 variables 加入导入路径，便于子脚本直接 import
sys.path.insert(0, str(HERE))

from variables import VARIABLES, resolve_preset, CMAPS  # noqa: E402

RESOLVE_OPTS = {"--parent": 1, "--refresh": 0, "--geojson": 1, "--list": 0}
FETCH_OPTS = {"--var": 1, "--agg": 1, "--year": 1, "--month": 1, "--date": 1,
              "--start": 1, "--end": 1, "--res": 1, "--buffer": 1, "--workers": 1}
NC_OPTS = {"--band": 1, "--bbox": 1, "--crs": 1, "--no-flip": 0, "--units": 1, "--agg": 1}
CLIP_OPTS = {"--no-convert": 0, "--strict": 0, "--fill": 0, "--nodata": 1, "--buffer": 1}
HTML_OPTS = {"--title": 1, "--period": 1, "--units": 1, "--legend": 1,
             "--colormap": 1, "--bg": 1, "--basemap": 1,
             "--cmap-type": 1, "--center": 1}


def split_opts(argv, table):
    picked, rest, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in table:
            n = table[a]
            picked += argv[i:i + 1 + n]
            i += 1 + n
        else:
            rest.append(a)
            i += 1
    return picked, rest


def val(opts, key):
    return opts[opts.index(key) + 1] if key in opts else None


def pop_val(opts, key):
    if key in opts:
        i = opts.index(key)
        v = opts[i + 1]
        del opts[i:i + 2]
        return v
    return None


def run(cmd, capture=False):
    print("+", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if capture and r.stdout:
        print(r.stdout, end="", flush=True)
    if capture and r.stderr:
        print(r.stderr, end="", file=sys.stderr, flush=True)
    if r.returncode != 0:
        sys.exit(r.returncode)


def run_parallel(cmds, capture=True):
    """并发跑多个子进程（彼此无数据依赖）。

    实测依据：HTML 预览与科研出图都只依赖**同一份裁剪后的 TIF**，无依赖关系。
    两者各自约 1.7 s / 3.4 s，其中大部分是**进程启动 + 模块导入**（实测裸启动
    741 ms、rasterio/matplotlib/scipy 各 0.6~1.1 s），因此重叠执行能直接省下
    较小者的全部墙钟时间 —— 实测串行 6.80 s → 并行 5.25 s（省 1.55 s，23%）。

    输出用 capture 收集后**按提交顺序**回放，避免多进程 stdout 交错成乱码。
    """
    from concurrent.futures import ThreadPoolExecutor
    for c in cmds:
        print("+", " ".join(c), flush=True)

    def _one(cmd):
        r = subprocess.run(cmd, capture_output=capture, text=True)
        return cmd, r

    with ThreadPoolExecutor(max_workers=len(cmds)) as ex:
        results = list(ex.map(_one, cmds))
    for cmd, r in results:
        if r.stdout:
            print(r.stdout, end="", flush=True)
        if r.stderr:
            print(r.stderr, end="", file=sys.stderr, flush=True)
        if r.returncode != 0:
            print(f"错误：子步骤失败（退出码 {r.returncode}）：{' '.join(cmd)}")
            sys.exit(r.returncode)


AGG_SUFFIX = {"sum": "累计", "mean": "平均", "min": "最小值", "max": "最大值"}


def build_period_text(preset, agg, year, month, date, start, end):
    label = preset["label"]
    suf = AGG_SUFFIX.get(agg, "")
    if start and end:
        return f"{start} ~ {end} {label}{suf}"
    if date:
        return f"{date} {label}"
    if month:
        return f"{year}年{month}月 {label}{suf}"
    return f"{year}年 {label}{suf}"


def _print_presets():
    """打印全部可用要素，让用户/调用方能直接看到可选类型。"""
    print("\n可用要素（--preset），共 %d 项：" % len(VARIABLES))
    for i, (k, v) in enumerate(VARIABLES.items(), 1):
        src = "open-meteo" + ("+nasa-power" if v.get("power_var") else "（POWER 不支持）")
        print(f"  {i:2d}. {k:<17} {v['label']:<12} {v['units'] or '—':<7}"
              f" 默认{v['agg']:<4} 数据源：{src}")
    print("  也可用 --var <Open-Meteo daily 变量名> 直接指定任意要素。\n")


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        _print_presets()
        sys.exit(0)
    if not argv:
        print(__doc__)
        _print_presets()
        sys.exit(1)
    name = argv[0]
    rest = argv[1:]
    workdir = Path("." if not rest or rest[0].startswith("--") else rest[0])
    if not (not rest or rest[0].startswith("--")):
        rest = rest[1:]
    workdir.mkdir(parents=True, exist_ok=True)

    rest = list(rest)
    # 数据源 / 出图控制（编排层参数，不转发给子脚本）
    source = pop_val(rest, "--source") or "open-meteo"
    no_figure = "--no-figure" in rest
    if no_figure:
        rest.remove("--no-figure")
    fig_dpi = pop_val(rest, "--dpi") or "300"
    fig_width = pop_val(rest, "--width-in") or "8"
    fig_grat = "--no-graticule" not in rest
    if "--no-graticule" in rest:
        rest.remove("--no-graticule")
    if source not in ("open-meteo", "nasa-power"):
        print("错误：--source 仅支持 open-meteo | nasa-power（或直接用 --nc 喂入 NetCDF）。")
        sys.exit(1)
    # 预设/变量
    preset_key = pop_val(rest, "--preset")
    var_direct = pop_val(rest, "--var")
    # 周期参数
    year = pop_val(rest, "--year")
    month = pop_val(rest, "--month")
    date = pop_val(rest, "--date")
    start = pop_val(rest, "--start")
    end = pop_val(rest, "--end")
    agg_override = pop_val(rest, "--agg")

    nc_file = pop_val(rest, "--nc")
    nc_opts, rest = split_opts(rest, NC_OPTS)
    r_opts, rest = split_opts(rest, RESOLVE_OPTS)
    f_opts, rest = split_opts(rest, FETCH_OPTS)
    c_opts, rest = split_opts(rest, CLIP_OPTS)
    h_opts, rest = split_opts(rest, HTML_OPTS)
    if rest:
        print("未识别的参数:", rest)

    # 解析预设
    # 用户给了 --preset 但不在注册表里时，明确列出全部可选项，避免"猜不到有哪些类型"
    if preset_key and preset_key not in VARIABLES:
        print(f"错误：未知要素 --preset {preset_key!r}。")
        print("      可选要素（--preset）如下，共 %d 项：" % len(VARIABLES))
        for i, (k, v) in enumerate(VARIABLES.items(), 1):
            print(f"        {i:2d}. {k:<17} {v['label']}"
                  f"（{v['units'] or '—'}，默认{v['agg']}）")
        print("      也可用 --var <Open-Meteo daily 变量名> 直接指定。")
        sys.exit(1)
    key, preset = resolve_preset(preset_key=preset_key, var_name=var_direct)
    var = preset["var"]
    units = preset["units"] or ""
    agg = (agg_override or preset["agg"]).lower()
    cmap_hex = ",".join(CMAPS.get(preset["cmap"], CMAPS["Viridis"]))

    # 周期文本
    period_text = build_period_text(preset, agg, year or "2023", month, date, start, end)

    # 文件名后缀：用预设 key（避免不同要素互相覆盖）
    suf = key if key != "custom" else (var_direct or "custom")
    geojson = workdir / f"{name}.json"
    raw = workdir / f"{name}_raw.tif"
    out = workdir / f"{name}_{suf}.tif"
    html = workdir / f"{name}_{suf}.html"
    py = sys.executable

    if "--title" not in h_opts:
        h_opts = h_opts + ["--title", name]
    if "--period" not in h_opts:
        h_opts = h_opts + ["--period", period_text]
    if "--units" not in h_opts:
        h_opts = h_opts + ["--units", units]
    if "--legend" not in h_opts:
        h_opts = h_opts + ["--legend", preset["label"]]
    if "--colormap" not in h_opts:
        h_opts = h_opts + ["--colormap", cmap_hex]
    if "--cmap-type" not in h_opts:
        h_opts = h_opts + ["--cmap-type", preset["cmap_type"]]
    if preset.get("zero_anchored") and "--zero-anchored" not in h_opts:
        h_opts = h_opts + ["--zero-anchored"]
    if preset.get("center") is not None and "--center" not in h_opts:
        h_opts = h_opts + ["--center", str(preset["center"])]

    if nc_file:
        # 始终确保边界落到 workdir/<name>.json：
        # - 传了 --geojson <自备文件> 时 resolve_boundary 会把它复制过来；
        # - 否则按区划名下载/查缓存。缺此步会导致后续 clip 找不到边界文件。
        run([py, str(HERE / "resolve_boundary.py"), name, str(geojson)] + r_opts)
        # read_nc 选项：变量取用户 --var（若给）或预设变量；--date/--units/--agg 一并带上
        nc_var = var_direct or var
        nc_run = list(nc_opts)
        if "--var" not in nc_run:
            nc_run += ["--var", nc_var]
        if "--units" not in nc_run:
            nc_run += ["--units", units]
        if "--agg" not in nc_run:
            nc_run += ["--agg", agg]
        if "--date" in f_opts:   # 用户可能用 --date 选 NetCDF 时间层（落在 f_opts 中）
            i = f_opts.index("--date")
            nc_run += ["--date", f_opts[i + 1]]
        run([py, str(HERE / "read_nc.py"), nc_file, str(raw)] + nc_run)
    else:
        run([py, str(HERE / "resolve_boundary.py"), name, str(geojson)] + r_opts)
        if source == "nasa-power":
            # 翻译预设到 POWER 参数；用户显式 --var 时按 POWER 参数名传入
            power_var = val(f_opts, "--var") or preset.get("power_var")
            if not power_var:
                print(f"错误：NASA POWER 暂不支持该要素（{key}）。"
                      f"请改用 --source open-meteo，或自备 NetCDF 用 --nc。")
                sys.exit(1)
            pfactor = preset.get("power_factor", 1.0) or 1.0
            punits = preset.get("power_units") or units
            units = punits  # 下游 html/figure 的单位以 POWER 为准
            f_power = ["--var", str(power_var), "--agg", agg,
                       "--units", punits, "--factor", str(pfactor)]
            for k, v in (("--res", "res"), ("--buffer", "buffer"), ("--workers", "workers")):
                pv = val(f_opts, k)
                if pv:
                    f_power += [k, pv]
            if year:
                f_power += ["--year", str(year)]
            if month:
                f_power += ["--month", str(month)]
            if date:
                f_power += ["--date", date]
            if start:
                f_power += ["--start", start]
            if end:
                f_power += ["--end", end]
            run([py, str(HERE / "fetch_power.py"), str(geojson), str(raw)] + f_power)
        else:
            # Open-Meteo：把变量/聚合/单位/单位参数注入 fetch 选项，并把周期参数补回
            f_fetch = list(f_opts) + ["--var", var, "--agg", agg,
                                      "--units", units, "--unit_params", preset.get("unit_params", "")]
            if year:
                f_fetch += ["--year", str(year)]
            if month:
                f_fetch += ["--month", str(month)]
            if date:
                f_fetch += ["--date", date]
            if start:
                f_fetch += ["--start", start]
            if end:
                f_fetch += ["--end", end]
            run([py, str(HERE / "fetch_climate.py"), str(geojson), str(raw)] + f_fetch)

    run([py, str(HERE / "clip_by_boundary.py"), str(raw), str(geojson), str(out)] + c_opts)

    # 科研级静态出图（matplotlib，离线自包含）
    fig = workdir / f"{name}_{suf}_figure.png"
    html_cmd = [py, str(HERE / "make_html.py"), str(out), str(geojson), str(html)] + h_opts
    if no_figure:
        # 跳过出图时无需并发，直接跑 HTML
        run(html_cmd)
    else:
        f_fig = ["--title", name, "--period", period_text, "--units", units,
                 "--legend", preset["label"], "--colormap", cmap_hex,
                 "--cmap-type", preset["cmap_type"], "--dpi", str(fig_dpi),
                 "--width-in", str(fig_width)]
        if preset.get("center") is not None:
            f_fig += ["--center", str(preset["center"])]
        if preset.get("zero_anchored"):
            f_fig += ["--zero-anchored"]
        if not fig_grat:
            f_fig += ["--no-graticule"]
        fig_cmd = [py, str(HERE / "make_figure.py"), str(out), str(geojson), str(fig)] + f_fig
        # HTML 与静态出图互不依赖（都只读裁剪后的 TIF），并发执行省时（实测 23%）。
        run_parallel([html_cmd, fig_cmd])

    print("\n成品:")
    for p in (out, html, fig, geojson, raw):
        if Path(p).exists():
            sz = Path(p).stat().st_size
            print(f"  {p}  ({sz/1048576:.2f} MB)" if sz > 1048576 else f"  {p}  ({sz} B)")


if __name__ == "__main__":
    main()
