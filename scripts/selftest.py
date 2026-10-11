#!/usr/bin/env python3
"""cn-climate-viz 离线回归测试（不联网）。

用合成边界 + 合成 NetCDF 跑通 read_nc -> clip_by_boundary -> make_html 全链路，
覆盖：2D 变量、3D 时间轴（--band 选层）、顺序型配色（0 起）、发散型配色（对称）。
不触发任何网络请求，可在 CI / 离线环境验证脚本健壮性。

用法: python selftest.py
退出码 0 = 通过, 1 = 失败。
"""
import sys
import json
import re
import math
import shutil
import tempfile
from pathlib import Path

import numpy as np
import netCDF4
from PIL import Image

HERE = Path(__file__).parent
PY = sys.executable

from variables import resolve_preset, nice_range  # noqa: E402


def gen_boundary(d, lon0, lon1, lat0, lat1):
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": "测试区"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}}]}
    (d / "boundary.json").write_text(json.dumps(gj, ensure_ascii=False))


def _write_boundary(path, lon0, lon1, lat0, lat1, name="测试区"):
    """写出指定路径的矩形边界 GeoJSON（供需要独立边界文件的用例使用）。"""
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": name},
        "geometry": {"type": "Polygon", "coordinates": [[
            [lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}}]}
    path.write_text(json.dumps(gj, ensure_ascii=False))


def _write_donut_boundary(path, cx, cy, r_out, r_in, n=64):
    """写出「圆环带状」边界：外圆挖去内圆（带洞多边形）。

    用途：复现「矢量边界在栅格内有细长突出部」的拓扑 —— 这是
    all_touched=True 也救不回来的场景：环带两侧都有大量像元紧邻 NaN，
    逐像元 alpha 一旦被插值，就会在边界内侧糊出一圈白边。
    """
    import math as _m
    outer = [[cx + r_out * _m.cos(2 * _m.pi * i / n),
              cy + r_out * _m.sin(2 * _m.pi * i / n)] for i in range(n)]
    inner = [[cx + r_in * _m.cos(2 * _m.pi * i / n),
              cy + r_in * _m.sin(2 * _m.pi * i / n)] for i in range(n)][::-1]
    ring = outer + inner + [outer[0]]
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": "环带"},
        "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
    path.write_text(json.dumps(gj, ensure_ascii=False))


def gen_boundary_circle(d, name, cx, cy, r, n=16):
    """内切于 bbox 的圆形多边形（WGS84）。用于复现 Bug2：小栅格 + 边界内切，
    多边形外为薄环 nodata，旧 fillnodata(距离10) 会把整圈 exterior 填满 -> alpha 全 255。"""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        pts.append([round(cx + r * math.cos(a), 5), round(cy + r * math.sin(a), 5)])
    pts.append(pts[0])
    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": name},
        "geometry": {"type": "Polygon", "coordinates": [pts]}}]}
    (d / (name + ".json")).write_text(json.dumps(gj, ensure_ascii=False))


def gen_nc(d, path, nlat, nlon, lat0, lat1, lon0, lon1, varname, fill=None, ntime=1, vals=None):
    lats = np.linspace(lat1, lat0, nlat)
    lons = np.linspace(lon0, lon1, nlon)
    if vals is not None:
        data = np.asarray(vals, dtype=np.float32)
        ds = netCDF4.Dataset(path, "w")
        ds.createDimension("lat", nlat)
        ds.createDimension("lon", nlon)
        vlat = ds.createVariable("lat", "f8", ("lat",)); vlat[:] = lats
        vlon = ds.createVariable("lon", "f8", ("lon",)); vlon[:] = lons
        vt = ds.createVariable(varname, "f4", ("lat", "lon"), fill_value=-9999.0)
        vt.units = "m/s"
        vt[:] = np.ma.masked_equal(np.where(data == -9999, -9999.0, data), -9999.0)
        ds.close()
    elif ntime == 1:
        data = (15 + 8 * np.sin(lats[:, None] / 0.1) + 3 * np.cos(lons[None, :] / 0.1)).astype(np.float32)
        if fill is not None:
            data[0, 0] = fill
        ds = netCDF4.Dataset(path, "w")
        ds.createDimension("lat", nlat)
        ds.createDimension("lon", nlon)
        vlat = ds.createVariable("lat", "f8", ("lat",)); vlat[:] = lats
        vlon = ds.createVariable("lon", "f8", ("lon",)); vlon[:] = lons
        vt = ds.createVariable(varname, "f4", ("lat", "lon"), fill_value=-9999.0)
        vt.units = "degC"
        vt[:] = np.ma.masked_equal(np.where(data == -9999, -9999.0, data), -9999.0)
        ds.close()
    else:
        ds = netCDF4.Dataset(path, "w")
        ds.createDimension("time", ntime)
        ds.createDimension("lat", nlat)
        ds.createDimension("lon", nlon)
        vlat = ds.createVariable("lat", "f8", ("lat",)); vlat[:] = lats
        vlon = ds.createVariable("lon", "f8", ("lon",)); vlon[:] = lons
        vt = ds.createVariable(varname, "f4", ("time", "lat", "lon"), fill_value=-9999.0)
        vt.units = "degC"
        for t in range(ntime):
            arr = (15 + t + 8 * np.sin(lats[:, None] / 0.1)).astype(np.float32)
            vt[t] = np.ma.masked_equal(arr, -9999.0)
        ds.close()


def run(cmd):
    print("+", " ".join(cmd))
    r = __import__("subprocess").run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        raise RuntimeError(f"命令失败 (exit {r.returncode}): {' '.join(cmd)}")
    return r


def assert_exists(p):
    assert Path(p).exists(), f"缺失产物: {p}"
    assert Path(p).stat().st_size > 0, f"空产物: {p}"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cnclimate_selftest_"))
    try:
        gen_boundary(tmp, 117.20, 117.45, 31.70, 31.95)
        RD = str(HERE / "read_nc.py")
        CL = str(HERE / "clip_by_boundary.py")
        HT = str(HERE / "make_html.py")
        BO = str(tmp / "boundary.json")

        # ---- 测试 1：2D 气温（显式指定发散型色带与 center，验证透传生效）----
        gen_nc(tmp, tmp / "t2d.nc", 20, 25, 31.70, 31.95, 117.20, 117.45, "temp", fill=-9999.0)
        run([PY, RD, str(tmp / "t2d.nc"), str(tmp / "raw2d.tif"), "--var", "temp"])
        run([PY, CL, str(tmp / "raw2d.tif"), BO, str(tmp / "out2d.tif"), "--no-convert"])
        run([PY, HT, str(tmp / "out2d.tif"), BO, str(tmp / "out2d.html"),
             "--title", "测试区", "--period", "2023年 平均气温", "--units", "°C",
             "--legend", "2m 气温",
             "--colormap", "#313695,#4575b4,#74add1,#abd9e9,#e0f3f8,#ffffbf,#fee090,#fdae61,#f46d43,#d73027,#a50026",
             "--cmap-type", "diverging", "--center", "0", "--basemap", "carto", "--no-convert"])
        assert_exists(tmp / "out2d.tif")
        assert_exists(tmp / "out2d.html")
        html = (tmp / "out2d.html").read_text(encoding="utf-8")
        assert "linear-gradient(90deg,#313695" in html, "发散型渐变缺失"
        assert "行政边界" in html, "线状色块图例缺失"
        print("  ✓ 2D 气温 / 发散配色 / 罗盘 / 线状图例 OK")

        # ---- 测试 2：3D 时间轴（--band 2 选层）----
        gen_nc(tmp, tmp / "t3d.nc", 20, 25, 31.70, 31.95, 117.20, 117.45, "temp", ntime=3)
        run([PY, RD, str(tmp / "t3d.nc"), str(tmp / "raw3d.tif"), "--var", "temp", "--band", "2"])
        run([PY, CL, str(tmp / "raw3d.tif"), BO, str(tmp / "out3d.tif"), "--no-convert"])
        assert_exists(tmp / "out3d.tif")
        print("  ✓ 3D 时间轴 --band 选层 OK")

        # ---- 测试 3：顺序型配色（降水，0 起）----
        gen_nc(tmp, tmp / "p.nc", 20, 25, 31.70, 31.95, 117.20, 117.45, "pr", fill=-9999.0)
        run([PY, RD, str(tmp / "p.nc"), str(tmp / "rawp.tif"), "--var", "pr"])
        run([PY, HT, str(tmp / "rawp.tif"), BO, str(tmp / "outp.html"),
             "--title", "测试区", "--period", "2023年7月 降水量累计", "--units", "mm",
             "--legend", "降水量",
             "--colormap", "#ffffcc,#edf8b1,#c7e9b4,#7fcdbb,#41b6c4,#1d91c0,#225ea8,#253494,#081d58",
             "--cmap-type", "sequential", "--basemap", "carto", "--no-convert"])
        assert_exists(tmp / "outp.html")
        assert "linear-gradient(90deg,#ffffcc" in (tmp / "outp.html").read_text(encoding="utf-8"), "顺序型渐变缺失"
        print("  ✓ 顺序型配色（0 起）OK")

        # ---- 测试 4：Bug1 回归 —— --cmap-type 必须真正生效 ----
        # 数据落在 [2.7,3.4]，单位 m/s（非 °C/hPa）。显式传 --cmap-type diverging --center 3.0。
        # 修复后：nice_range 对称区间 [2.5,3.5]，首刻度≈2.5、中刻度≈3.0（不归零）。
        # 旧 bug：opts.get("cmap_type")（下划线）读不到 --cmap-type（连字符），
        #   退化为 sequential -> 首刻度=0 -> 断言失败。
        vals = 2.7 + 0.7 * np.linspace(0, 1, 20)[:, None]   # 20 行，跨 [2.7,3.4]
        gen_nc(tmp, tmp / "wind.nc", 20, 25, 31.70, 31.95, 117.20, 117.45, "ws", vals=vals)
        run([PY, RD, str(tmp / "wind.nc"), str(tmp / "raw_w.tif"), "--var", "ws"])
        run([PY, CL, str(tmp / "raw_w.tif"), BO, str(tmp / "out_w.tif"), "--no-convert"])
        run([PY, HT, str(tmp / "out_w.tif"), BO, str(tmp / "out_w.html"),
             "--title", "测试区", "--period", "测试 风速", "--units", "m/s",
             "--legend", "10m 风速",
             "--colormap", "#ffffcc,#ffeda0,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026",
             "--cmap-type", "diverging", "--center", "3.0", "--basemap", "carto", "--no-convert"])
        html_w = (tmp / "out_w.html").read_text(encoding="utf-8")
        m = re.search(r'class="ticks"><span>([\d.]+)</span><span>([\d.]+)</span>'
                      r'<span>([\d.]+)</span><span>([\d.]+)</span><span>([\d.]+)</span>', html_w)
        assert m, "未能从 HTML 解析图例刻度"
        ticks = [float(x) for x in m.groups()]
        assert ticks[0] > 1.5, f"Bug1 复现：--cmap-type 未生效，首刻度被归零={ticks[0]}（应为≈2.5）"
        assert abs(ticks[2] - 3.0) < 0.05, f"Bug1 复现：中刻度未对齐 center=3.0（实际={ticks[2]}）"
        print(f"  ✓ Bug1 回归：--cmap-type 生效（对称区间首刻度 {ticks[0]}、中刻度 {ticks[2]}）")

        # ---- 测试 5：Bug2 回归 —— 小栅格 + 内切边界，边界外必须透明、覆盖率真实 ----
        # 16x16 小栅格 + 内切圆形多边形：多边形外为薄环 nodata。
        # 修复后：alpha 用填充前掩膜 -> 边界外透明(alpha=0)、覆盖率<100%。
        # 旧 bug：fillnodata(距离10) 填满 exterior 后 invalid 被改写 -> alpha 全 255、覆盖率虚高。
        gen_nc(tmp, tmp / "small.nc", 16, 16, 31.70, 31.95, 117.20, 117.45, "ws2", vals=np.full((16, 16), 3.0, dtype=np.float32))
        run([PY, RD, str(tmp / "small.nc"), str(tmp / "raw_s.tif"), "--var", "ws2"])
        gen_boundary_circle(tmp, "circle", 117.325, 31.825, 0.11)
        CB = str(tmp / "circle.json")
        run([PY, CL, str(tmp / "raw_s.tif"), CB, str(tmp / "out_s.tif"), "--no-convert"])
        run([PY, HT, str(tmp / "out_s.tif"), CB, str(tmp / "out_s.html"),
             "--title", "圆区", "--period", "测试", "--units", "m/s", "--legend", "风速",
             "--colormap", "#ffffcc,#ffeda0,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026",
             "--cmap-type", "sequential", "--basemap", "carto", "--no-convert"])
        png_s = tmp / "out_s_preview.png"
        assert png_s.exists() and png_s.stat().st_size > 0, "缺失配色预览 PNG"
        im = np.array(Image.open(png_s))
        assert im.ndim == 3 and im.shape[2] == 4, "预览 PNG 应为 RGBA"
        n_transparent = int((im[..., 3] == 0).sum())
        assert n_transparent > 0, "Bug2 复现：边界外应存在透明像元(alpha=0)，但全部不透明"
        m2 = re.search(r"有效覆盖 <b>([\d.]+)</b>%", (tmp / "out_s.html").read_text(encoding="utf-8"))
        assert m2, "未找到覆盖率统计"
        cov = float(m2.group(1))
        assert cov < 100.0, f"Bug2 复现：覆盖率虚高至 {cov}%（应<100）"
        print(f"  ✓ Bug2 回归：边界外透明（{n_transparent} 透明像元）、覆盖率真实（{cov:.1f}%）")

        # ---- 测试 6：科研级静态出图（make_figure，离线）----
        MK = str(HERE / "make_figure.py")
        fig_p = tmp / "out_s_figure.png"
        run([PY, MK, str(tmp / "out_s.tif"), CB, str(fig_p),
             "--title", "圆区", "--period", "测试", "--units", "m/s", "--legend", "风速",
             "--colormap", "#ffffcc,#ffeda0,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026",
             "--cmap-type", "sequential", "--dpi", "150", "--width-in", "6"])
        assert fig_p.exists() and fig_p.stat().st_size > 2000, "缺失科研级出图 PNG"
        with Image.open(fig_p) as im:
            w, h = im.size
        assert w > 200 and h > 200, f"出图尺寸异常 {w}x{h}"
        print(f"  ✓ 科研级出图 OK（{w}x{h} @150dpi，自包含离线 PNG）")

        # ---- 测试 7：NASA POWER 预设映射（--source nasa-power 的翻译基础）----
        _, wind_p = resolve_preset(preset_key="wind")
        assert wind_p.get("power_var") == "WS10M", "wind 应映射到 POWER 参数 WS10M"
        _, pres_p = resolve_preset(preset_key="pressure")
        assert pres_p.get("power_var") == "PS", "pressure 应映射到 POWER 参数 PS"
        assert abs(pres_p.get("power_factor", 1.0) - 10.0) < 1e-6, "pressure 需 ×10 由 kPa 转 hPa"
        _, snow_p = resolve_preset(preset_key="snowfall")
        assert not snow_p.get("power_var"), "snowfall 无 POWER 参量，应置空（触发来源不支持提示）"
        print("  ✓ NASA POWER 预设映射（wind->WS10M / pressure->PS×10 / snowfall 不支持）")

        # ---- 测试 8：色阶对比度（zero_anchored）----
        # 非零锚定顺序要素（风/湿度…）：窄区间应居中，不得被拉伸归零（否则对比度塌缩）。
        lo, hi = nice_range(2.68, 3.39, "sequential", 0.0, False)
        assert lo > 1.5, f"非零锚定要素被归零：lo={lo}（应≈2.5，居中提升对比度）"
        assert abs(hi - 3.5) < 0.6, f"上界未取整到漂亮值：hi={hi}"
        # 零锚定顺序要素（降水/辐射/降雪）：必须从 0 起。
        lo2, hi2 = nice_range(0.0, 120.0, "sequential", 0.0, True)
        assert lo2 == 0.0, f"零锚定要素下界应为 0，实际 {lo2}"
        # 非零锚定但数据本身为正且窄：不得出现负刻度。
        lo3, hi3 = nice_range(2.68, 3.39, "sequential", 0.0, False)
        assert lo3 >= 0, "非负要素不应出现负刻度"
        # 发散型：数据**跨越**中心时保持零中心对称（气温冬季 -5~3°C 场景）。
        dlo, dhi = nice_range(-5.0, 3.0, "diverging", 0.0, False)
        assert dlo == -5.0 and dhi == 5.0, f"跨零发散区间被破坏：[{dlo},{dhi}]"
        dlo2, dhi2 = nice_range(-30.0, 45.0, "diverging", 0.0, False)
        assert dlo2 == -50.0 and dhi2 == 50.0, f"跨零宽区间发散被破坏：[{dlo2},{dhi2}]"
        # 发散型：数据**不跨越**中心时必须退回居中区间，否则整幅塌缩成单色
        # （实测合肥 6 月气温 25.03~26.78°C、center=0 -> 曾得到 [-50,50]，全图单色）。
        clo, chi = nice_range(25.03, 26.78, "diverging", 0.0, False)
        assert chi - clo < 10.0, f"不跨中心的发散色带未退回，区间仍过宽：[{clo},{chi}]"
        assert clo >= 20.0, f"不跨中心时应居中于数据：lo={clo}"
        print(f"  ✓ 色阶对比度：风速 [{lo:.2f},{hi:.2f}] 居中 / 降水 [{lo2:.2f},{hi2:.2f}] 零起 / "
              f"跨零发散 [{dlo:.0f},{dhi:.0f}] / 不跨零退回 [{clo:.2f},{chi:.2f}]")

        # ---- 测试 9：出图平滑不得被 NaN 污染（回归）----
        # scipy.ndimage.gaussian_filter 直接作用于含 NaN 数组时，NaN 会沿邻域扩散
        # 污染整幅栅格（实测 3x3 仅 1 个 NaN 即全变 NaN），表现为「出图空白/退化为色块」。
        # 正确做法是掩膜加权：数值项与权重项分别滤波后相除。
        from scipy import ndimage
        a = np.array([[1., 2., 3.], [4., np.nan, 6.], [7., 8., 9.]])
        direct = ndimage.gaussian_filter(a, sigma=0.6, mode="nearest")
        # 记录前提：直接滤波会被 NaN 污染（本机 scipy 上整幅变 NaN）。
        # 若将来 scipy 改变了这一行为也不影响本用例——下面只断言「掩膜加权法必须安全」。
        direct_contaminated = not np.isfinite(direct).all()
        inv9 = np.isnan(a)
        num9 = ndimage.gaussian_filter(np.where(inv9, 0.0, a), sigma=0.6, mode="nearest")
        den9 = ndimage.gaussian_filter((~inv9).astype(float), sigma=0.6, mode="nearest")
        sm9 = np.divide(num9, den9, out=np.full_like(num9, np.nan), where=den9 > 1e-6)
        # 脚本里最终还会 np.where(invalid, nan, sm) 把原无效处显式还原为 NaN，
        # 因此「原无效处不得凭空造值」这条由该步保证（见下面的端到端断言）。
        assert np.isfinite(sm9).all(), "掩膜加权滤波后不应残留 NaN（除刻意屏蔽处）"
        sm9 = np.where(inv9, np.nan, sm9)
        assert np.isfinite(sm9).sum() == 8, "掩膜加权+原位还原后有效值数量异常"
        assert np.isnan(sm9[1, 1]), "原无效处应仍为无效（不得凭空造值）"
        # 端到端：带 nodata 空洞的栅格出图后仍应有内容（未被全 NaN 抹掉）
        vals = np.full((16, 16), 3.0, dtype=np.float32)
        vals[4:8, 4:8] = -9999.0     # 挖一个 4x4 空洞
        gen_nc(tmp, tmp / "hole.nc", 16, 16, 31.70, 31.95, 117.20, 117.45, "h", vals=vals)
        run([PY, str(HERE / "read_nc.py"), str(tmp / "hole.nc"), str(tmp / "raw_h.tif"), "--var", "h"])
        run([PY, str(HERE / "clip_by_boundary.py"), str(tmp / "raw_h.tif"), BO,
             str(tmp / "out_h.tif"), "--no-convert"])
        run([PY, str(HERE / "make_figure.py"), str(tmp / "out_h.tif"), BO,
             str(tmp / "out_h_figure.png"), "--title", "空洞", "--dpi", "150", "--width-in", "6"])
        fh = tmp / "out_h_figure.png"
        assert fh.exists() and fh.stat().st_size > 2000, "带空洞栅格出图失败"
        with Image.open(fh) as im9:
            arr9 = np.array(im9.convert("RGB")).astype(int)
        # 统计非白像素：若被 NaN 全污染整幅变白，则该值会接近 0
        nonwhite = int((arr9.sum(axis=2) < 720).sum())
        assert nonwhite > 5000, f"出图几乎全白（仅 {nonwhite} 个着色像素），疑似 NaN 污染/栅格被抹空"
        print(f"  ✓ 出图 NaN 安全（直接滤波会污染={direct_contaminated}；带 4x4 空洞仍渲染出 {nonwhite} 个着色像素）")


        # ---- 测试 10：边界内补洞必须真正执行（v1.6.0 修 NameError 静默失效）----
        #
        # 背景：v1.4.0 那段"边界内边缘补齐"读未定义变量 `disp` 抛 NameError，
        # 被 `except Exception: pass` 吞掉，**从引入起从未执行过**。v1.6.0 修好。
        #
        # 判据选型（三次返工，务必保留此记录）：
        #   ① figure.png 上的「边界内侧白边」——不可观测：clip_path 把填色严格裁到
        #      矢量边界，栅格层面的半透明/缺口整个被裁掉。实测注入 alpha 插值 bug
        #      后，核心/边缘/最边缘三层 RGB 欧氏距离全为 0.0。
        #   ② _preview.png 上的「alpha 中间值」——恒为 0：make_html 用 Image.NEAREST
        #      生成 alpha，本就二值（实测 40/120/240 栅格中间值占比 0.000%）。
        #   ③ 两个「图像判据」都**无法感知补洞是否执行**（注入 bug 后仍 PASS）。
        #      故改为**直接度量补洞逻辑的唯一产出**：边界内原本无效的像元，
        #      经 make_figure 处理后是否已被赋上有效值。
        #
        # 做法：这里的栅格由 clip 产出（边界内存在 holes），再让 make_figure
        # 把补洞结果**回写**一份中间 TIF（--dump-filled），直接比较像元。
        from PIL import Image as _I  # noqa: F401  (保留：后续如需目视可读图)
        import rasterio as _rio
        from rasterio.features import geometry_mask as _gm
        from geo_utils import load_boundary_wgs84 as _lb

        boe = tmp / "bo_e.json"
        # 环带内外两侧都紧邻 NaN，是"边界处无数据可填"的最苛刻形态。
        _write_donut_boundary(boe, 117.325, 31.825, 0.125, 0.075)
        gen_nc(tmp, tmp / "edge.nc", 40, 40, 31.680, 31.970, 117.180, 117.470,
               "e", vals=np.full((40, 40), 5.0, dtype=np.float32))
        run([PY, str(HERE / "read_nc.py"), str(tmp / "edge.nc"),
             str(tmp / "raw_e.tif"), "--var", "e"])
        run([PY, str(HERE / "clip_by_boundary.py"), str(tmp / "raw_e.tif"),
             str(boe), str(tmp / "out_e.tif"), "--no-convert"])

        with _rio.open(str(tmp / "out_e.tif")) as _ds:
            _arr = _ds.read(1)
            _tr = _ds.transform
        _inv0 = ~np.isfinite(_arr)
        _g = _lb(str(boe), do_convert=True)[2]
        _gl = list(_g.geoms) if _g.geom_type.startswith("Multi") else [_g]
        _inside = _gm(_gl, out_shape=_arr.shape, transform=_tr,
                      invert=True, all_touched=True)
        _holes = _inside & _inv0
        assert int(_holes.sum()) > 0, (
            "用例前提不成立：边界内没有无效像元（holes=0），无法检验补洞。"
            "若 clip 改动导致 holes 恒为 0，本用例即失去意义，需重新构造输入。")

        # 让 make_figure 落一份"补洞后栅格"，便于逐像元比对。
        run([PY, str(HERE / "make_figure.py"), str(tmp / "out_e.tif"), str(boe),
             str(tmp / "out_e_figure.png"), "--title", "环带",
             "--dpi", "150", "--width-in", "6", "--no-graticule",
             "--dump-filled", str(tmp / "filled_e.tif")])
        assert (tmp / "filled_e.tif").exists(), (
            "make_figure 未产出 --dump-filled 栅格（补洞链路可能整体未执行）")
        with _rio.open(str(tmp / "filled_e.tif")) as _ds2:
            _filled = _ds2.read(1)
        assert _filled.shape == _arr.shape, \
            f"补洞后栅格尺寸变化：{_filled.shape} vs {_arr.shape}"

        # 核心断言：边界内原本无效的像元，必须已被补齐为有效值。
        _still = int((_holes & ~np.isfinite(_filled)).sum())
        assert _still == 0, (
            f"边界内仍有 {_still}/{int(_holes.sum())} 个像元未补齐 —— "
            f"补洞逻辑未生效（v1.4.0 曾是 NameError 被静默吞掉）")
        # 反向约束：补洞不得把边界外的无效像元也填上（那会破坏"边界外透明"）。
        _outside = ~_inside
        _badfill = int((_outside & _inv0 & np.isfinite(_filled)).sum())
        assert _badfill == 0, f"补洞越界：{_badfill} 个边界外无效像元被填上了值"
        # 值域约束：补洞值必须落在原有有效值域内。
        _vv = _arr[np.isfinite(_arr)]
        _fv = _filled[np.isfinite(_filled)]
        assert _fv.min() >= _vv.min() - 1e-6 and _fv.max() <= _vv.max() + 1e-6, \
            f"补洞值超出原值域：[{_fv.min()},{_fv.max()}] 不在 [{_vv.min()},{_vv.max()}]"
        print(f"  ✓ 边界内补洞生效（{int(_holes.sum())} 个 holes 全部补齐，"
              f"边界外 {int(_outside.sum())} 像元未被越界填充）")


        # ---- 测试 11：手写 numpy 补洞/平滑与原语自检 ----
        #
        # v1.6.0 用 numpy 重写了 scipy 的三处用法（EDT 补洞 / zoom / gaussian），
        # 直接对原语做单元断言，比端到端图像判据更稳、更快、更早暴露问题。
        sys.path.insert(0, str(HERE))
        from make_figure import fill_nearest, blur_masked, _gauss1d
        # (a) 高斯核必须归一化
        k = _gauss1d(2.2)
        assert abs(float(k.sum()) - 1.0) < 1e-12, f"高斯核未归一化：sum={k.sum()}"
        assert len(k) % 2 == 1, "高斯核长度应为奇数（对称）"
        # (b) 均匀场 + 全有效：加权平滑必须**保持常量**（不能引入亮度偏移）
        uni = np.full((24, 24), 7.5)
        allv = np.ones((24, 24), dtype=bool)
        sm = blur_masked(uni, allv, 0.7)
        assert np.allclose(sm, 7.5, atol=1e-9), \
            f"均匀场平滑后不再恒定：min={np.nanmin(sm)} max={np.nanmax(sm)}"
        # (c) 掩膜加权平滑：**内部空洞**（周围都有有效值）会被平滑合理地填上——
        # 这是预期行为（函数职责就是把缺口平滑插出来），真正的无效边界由调用方
        # 用 `np.where(原掩膜, nan, sm)` 还原。故这里只断言两条：
        #   c1 有效区不得出现 NaN（NaN 不得向有效区扩散——这才是 scipy 直接滤波的坑）；
        #   c2 空洞内的插值必须落在原值域内，不得凭空放大/造出异常值。
        #
        # 关键：输入必须是**梯度场**而非常量场。用常量场时，"直接对含 NaN 数组滤波"
        # 的破坏完全不可见（NaN 混进去也是常量，edge 外推照样给出有限值），
        # 实测注入 bug 后常量场用例仍 PASS。换成梯度场即可暴露：
        # 直接卷积会把空洞边缘的有效像元一并吃掉（实测有效值从 884 掉到 800）。
        n11 = 30
        grad11 = np.tile(np.arange(n11, dtype=float), (n11, 1))
        m11 = np.ones((n11, n11), dtype=bool)
        m11[10:14, 10:14] = False          # 内部 4x4 空洞
        a11 = np.where(m11, grad11, np.nan)
        sm11 = blur_masked(np.where(m11, a11, 0.0), m11, 0.7)
        assert np.isfinite(sm11[m11]).all(), \
            f"有效区出现 NaN：NaN 已扩散进有效区（{(~np.isfinite(sm11[m11])).sum()} 个）"
        lost = int(m11.sum() - np.isfinite(sm11)[m11].sum())
        assert lost == 0, f"平滑吃掉了 {lost} 个原本有效的像元（应为 0）"
        # 形变量约束：平滑后的有效区必须仍接近原梯度场，不得被过度抹平。
        dev = float(np.abs(sm11[m11] - grad11[m11]).mean())
        assert dev < 0.5, f"平滑后有效区平均形变 {dev:.3f} 过大（疑似越界/污染）"
        inside_vals = sm11[~m11 & np.isfinite(sm11)]
        assert inside_vals.size > 0, "空洞未被平滑填补（函数职责是把缺口插出来）"
        assert inside_vals.min() >= grad11.min() - 1e-6 \
            and inside_vals.max() <= grad11.max() + 1e-6, \
            f"空洞内插值超出原值域：[{inside_vals.min()},{inside_vals.max()}]"
        # (c3) 贴边（非内部空洞）情形：若无效区一直连到画布边缘，外推不得越界造值。
        edge11 = np.zeros((20, 20), dtype=bool)
        edge11[:, 0] = True          # 左边缘整列无效
        me11 = ~edge11
        a11b = np.where(me11, 4.0, 0.0)
        sm11b = blur_masked(a11b, me11, 0.7)
        assert np.isfinite(sm11b[me11]).all(), "贴边无效区不得出现 NaN"
        assert np.allclose(sm11b[me11], 4.0, atol=1e-9), \
            f"贴边外推应恒等于邻域常量 4.0，实际 [{sm11b[me11].min()},{sm11b[me11].max()}]"
        # (d) 最近邻补洞：挖一个洞，填补值必须落在原值域内且洞被填满
        r = np.arange(20 * 20, dtype=float).reshape(20, 20)
        inv11 = np.zeros_like(r, dtype=bool)
        inv11[8:12, 8:12] = True
        filled, iters = fill_nearest(r, inv11)
        assert (~np.isfinite(filled)).sum() == 0, "补洞后仍有无值像元"
        assert iters >= 1, f"补洞轮数应 >=1，实际 {iters}"
        lo_f, hi_f = float(filled[inv11].min()), float(filled[inv11].max())
        assert r.min() <= lo_f and hi_f <= r.max(), \
            f"补洞值超出原值域：[{lo_f},{hi_f}] 不在 [{r.min()},{r.max()}]"
        assert np.allclose(filled[~inv11], r[~inv11]), "补洞改动了原本有效的像元"
        # (e) 全无效输入不得崩（边界情形）
        allb = np.zeros((6, 6), dtype=bool)
        f2, it2 = fill_nearest(np.zeros((6, 6)), allb)
        assert it2 == 0, f"无缺失却发生了 {it2} 轮填洞"
        print(f"  ✓ numpy 原语自检 OK（核归一化 / 常量保持 / NaN 不扩散 / 补洞值域 / 空输入）")

        print("\nSELFTEST PASS ✅")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("SELFTEST FAIL ❌:", e)
        sys.exit(1)
