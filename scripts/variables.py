#!/usr/bin/env python3
"""多气象要素预设注册表（variable presets）+ 通用科研配色。

本模块把「变量」从气温扩展为一套可插拔的要素库：
- 每个预设描述 Open-Meteo Archive 的 daily 变量名、单位、默认聚合方式
  （mean/sum/min/max）、ColorBrewer 调色板、配色类型（diverging/sequential）。
- 同时提供变量无关的 colorize() 与 nice_range()，make_html.py 据此上色与画图例。

新增要素只需在 VARIABLES 里加一项；Open-Meteo 支持的 daily 变量名见
https://open-meteo.com/en/docs/history-weather-api 。
"""
import math

import numpy as np

# ---------------------------------------------------------------------------
# ColorBrewer 调色板（十六进制，自冷/浅到暖/深）。顺序即数值由低到高。
# ---------------------------------------------------------------------------
CMAPS = {
    # 气温：绝对量用**顺序**色带（浅黄 -> 深红，"越热越深"），不用 diverging。
    # 理由：气温是绝对量不是距平，0 °C 中心无物理意义；用发散色带时夏季数据全在
    # 0 °C 以上，会把全部像元压进色带同一小段、整幅图退化为单色（实测合肥 6 月 25~27 °C）。
    "YlOrRd_t": ["#fff7ec", "#fee8c8", "#fdd49e", "#fdbb84", "#fc8d59",
                 "#ef6548", "#d7301f", "#990000"],
    # 降水 / 积雪：浅黄绿 -> 深蓝（sequential，0 起）
    "YlGnBu": ["#ffffcc", "#edf8b1", "#c7e9b4", "#7fcdbb", "#41b6c4",
               "#1d91c0", "#225ea8", "#253494", "#081d58"],
    "PuBu": ["#fff7fb", "#ece7f2", "#d0d1e6", "#a6bddb", "#67a9cf",
             "#3690c0", "#02818a", "#016450", "#014636"],
    # 风 / 辐射：浅黄 -> 深红（sequential，0 起）
    "YlOrRd": ["#ffffcc", "#ffeda0", "#fed976", "#feb24c", "#fd8d3c",
               "#f03b20", "#bd0026"],
    # 湿度：浅绿 -> 深蓝（sequential，0 起）
    "GnBu": ["#f7fcf0", "#e0f3db", "#ccebc5", "#a8ddb5", "#7bccc4",
             "#4eb3d3", "#2b8cbe", "#0868ac", "#084081"],
    # 气压：发散（紫<->橙），center 取数据中点
    "Spectral": ["#9e0142", "#d53e4f", "#f46d43", "#fdae61", "#fee08b",
                 "#ffffbf", "#e6f598", "#abdda4", "#66c2a5", "#3288bd", "#5e4fa2"],
    # 云量：白 -> 深灰（sequential，0 起，值越大越阴）
    "Greys": ["#ffffff", "#f0f0f0", "#d9d9d9", "#bdbdbd", "#969696",
              "#737373", "#525252", "#252525"],
    # 能见度：浅黄 -> 深绿（sequential，0 起，值越大越清晰）
    "YlGn": ["#ffffe5", "#f7fcb9", "#d9f0a3", "#addd8e", "#78c679",
             "#41ab5d", "#238443", "#006837"],
    # 通用回退
    "Viridis": ["#440154", "#482878", "#3e4989", "#31688e", "#26828e",
                "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725"],
}

# 预设：key -> 信息。agg 默认聚合；cmap_type 决定图例区间是否对称发散。
# unit_params 为附加到 Open-Meteo URL 的单位参数（让返回单位更友好）。
VARIABLES = {
    "temperature": {
        "label": "2m 气温", "en": "2m Temperature",
        "var": "temperature_2m_mean", "units": "°C", "agg": "mean",
        "cmap": "YlOrRd_t", "cmap_type": "sequential", "center": None,
        "unit_params": "", "zero_anchored": False,
        "power_var": "T2M", "power_units": "°C",
    },
    "temperature_max": {
        "label": "2m 最高气温", "en": "2m Max Temperature",
        "var": "temperature_2m_max", "units": "°C", "agg": "mean",
        "cmap": "YlOrRd_t", "cmap_type": "sequential", "center": None,
        "unit_params": "", "zero_anchored": False,
        "power_var": "T2M_MAX", "power_units": "°C",
    },
    "temperature_min": {
        "label": "2m 最低气温", "en": "2m Min Temperature",
        "var": "temperature_2m_min", "units": "°C", "agg": "mean",
        "cmap": "PuBu", "cmap_type": "sequential", "center": None,
        "unit_params": "", "zero_anchored": False,
        "power_var": "T2M_MIN", "power_units": "°C",
    },
    "precipitation": {
        "label": "降水量", "en": "Precipitation",
        "var": "precipitation_sum", "units": "mm", "agg": "sum",
        "cmap": "YlGnBu", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "&precipitation_unit=mm", "zero_anchored": True,
        "power_var": "PRECTOT", "power_units": "mm",
    },
    "snowfall": {
        "label": "降雪量", "en": "Snowfall",
        "var": "snowfall_sum", "units": "cm", "agg": "sum",
        "cmap": "PuBu", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "", "zero_anchored": True,
        # POWER 无独立降雪参量（PRECTOT 含雨雪合计），故不提供 power_var
    },
    "wind": {
        "label": "10m 风速", "en": "10m Wind Speed",
        "var": "wind_speed_10m_mean", "units": "m/s", "agg": "mean",
        "cmap": "YlOrRd", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "&wind_speed_unit=m/s", "zero_anchored": False,
        "power_var": "WS10M", "power_units": "m/s",
    },
    "wind_max": {
        "label": "10m 最大风速", "en": "10m Wind Gust",
        "var": "wind_speed_10m_max", "units": "m/s", "agg": "mean",
        "cmap": "YlOrRd", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "&wind_speed_unit=m/s", "zero_anchored": False,
        "power_var": "WS10M_MAX", "power_units": "m/s",
    },
    "humidity": {
        "label": "2m 相对湿度", "en": "2m Relative Humidity",
        "var": "relative_humidity_2m_mean", "units": "%", "agg": "mean",
        "cmap": "GnBu", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "", "zero_anchored": False,
        "power_var": "RH2M", "power_units": "%",
    },
    "pressure": {
        "label": "海平面气压", "en": "Mean Sea Level Pressure",
        "var": "pressure_msl_mean", "units": "hPa", "agg": "mean",
        "cmap": "Spectral", "cmap_type": "diverging", "center": None,
        "unit_params": "", "zero_anchored": False,
        # POWER 的 PS 为海平面气压（kPa），需 ×10 转 hPa
        "power_var": "PS", "power_units": "hPa", "power_factor": 10.0,
    },
    "radiation": {
        "label": "短波辐射", "en": "Shortwave Radiation",
        "var": "shortwave_radiation_sum", "units": "MJ/m²", "agg": "sum",
        "cmap": "YlOrRd", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "", "zero_anchored": True,
        "power_var": "ALLSKY_SFC_SW_DWN", "power_units": "MJ/m²",
    },
    "cloud": {
        "label": "云量", "en": "Cloud Cover",
        "var": "cloud_cover_mean", "units": "%", "agg": "mean",
        "cmap": "Greys", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "", "zero_anchored": False,
        # POWER 无稳定云量参量，故不提供 power_var
    },
    "visibility": {
        "label": "能见度", "en": "Visibility",
        "var": "visibility_mean", "units": "km", "agg": "mean",
        "cmap": "YlGn", "cmap_type": "sequential", "center": 0.0,
        "unit_params": "&visibility_unit=km", "zero_anchored": False,
        # POWER 无能见度参量，故不提供 power_var
    },
}

# 通过 Open-Meteo daily 变量名反查预设 key（用于 --var 直接传变量名时推断预设）
_VAR_TO_PRESET = {v["var"]: k for k, v in VARIABLES.items()}


def resolve_preset(preset_key=None, var_name=None):
    """返回 (key, preset_dict)。

    - 优先用 preset_key（必须在 VARIABLES 中）；
    - 否则用 var_name：若在已知变量中则取其预设，否则构造一个通用预设
      （Viridis 配色、单位未知、聚合 mean）。
    """
    if preset_key and preset_key in VARIABLES:
        return preset_key, dict(VARIABLES[preset_key])
    if var_name:
        if var_name in _VAR_TO_PRESET:
            k = _VAR_TO_PRESET[var_name]
            return k, dict(VARIABLES[k])
        # 未知变量：通用回退
        return "custom", {
            "label": var_name, "en": var_name, "var": var_name,
            "units": "", "agg": "mean", "cmap": "Viridis",
            "cmap_type": "sequential", "center": 0.0, "unit_params": "",
        }
    # 默认气温
    return "temperature", dict(VARIABLES["temperature"])


def hex_to_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _nice_ceil(x):
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    f = x / base
    nf = 1.0 if f <= 1 else 2.0 if f <= 2 else 2.5 if f <= 2.5 else (
        5.0 if f <= 5 else 10.0)
    return nf * base


def nice_range(vmin, vmax, cmap_type="sequential", center=None, zero_anchored=False):
    """把 [vmin, vmax] 扩展成漂亮的图例区间 (lo, hi)。

    - diverging：默认以 center 为中心左右对称（center=None 时取数据中点），
      上下界取整到 1/2/5×10^k。
      但当数据**不跨越 center** 时（如盛夏气温 25~27°C、center=0），零中心对称区间会把
      全部像元压进色带的同一小段，整幅图退化为单色。此时自动退回「居中对称漂亮区间」
      （等价于非零锚定），保证窄区间仍有对比度。
    - sequential + zero_anchored（降水/辐射/降雪等真零值要素）：下界 0，上界向上取整到漂亮值。
    - sequential + 非 zero_anchored（风/湿度/云量/能见度等不从 0 起）：
      **以数据中点为中心、取对称漂亮半宽**，避免把窄区间拉伸到 [0, n] 导致对比度塌缩
      （如风速 2.7~3.4 若强行 [0,5] 会被压扁，居中后约 [2.5,3.5] 对比清晰）。
    无效输入回退到 (0, 1)。
    """
    if not (np.isfinite(vmin) and np.isfinite(vmax)) or vmax <= vmin:
        return 0.0, 1.0
    if cmap_type == "diverging":
        c = (vmin + vmax) / 2.0 if center is None else float(center)
        if not (vmin <= c <= vmax):
            # 数据不跨越发散中心：零中心对称会塌缩成单色，退回居中对称区间
            mid = (vmin + vmax) / 2.0
            half = _nice_ceil((vmax - vmin) / 2.0)
            lo, hi = mid - half, mid + half
            if lo < 0 and vmin >= 0:
                lo, hi = 0.0, _nice_ceil(vmax)
            return lo, hi
        half = max(abs(vmax - c), abs(vmin - c))
        half = _nice_ceil(half)
        return c - half, c + half
    # sequential
    if zero_anchored:
        return 0.0, _nice_ceil(vmax)
    # 非零锚定：居中对称区间，提升窄区间对比度
    mid = (vmin + vmax) / 2.0
    half = _nice_ceil((vmax - vmin) / 2.0)
    lo = mid - half
    hi = mid + half
    # 非负要素（如风速）不应出现负刻度：下界被压到 0 时退化为 [0, nice(vmax)]
    if lo < 0 and vmin >= 0:
        lo = 0.0
        hi = _nice_ceil(vmax)
    return lo, hi


def colorize(arr, cmap_name, vmin, vmax):
    """把数组按调色板 cmap_name 上色，返回 uint8 RGB（与 arr 同形状 + (3,)）。

    nodata（NaN）保持 (0,0,0)，由调用方据掩膜转透明。
    """
    hexes = CMAPS.get(cmap_name, CMAPS["Viridis"])
    rgb_stops = [hex_to_rgb(h) for h in hexes]
    stops = len(rgb_stops)
    a = np.asarray(arr, dtype=np.float32)
    out = np.zeros(a.shape + (3,), dtype=np.float32)
    invalid = ~np.isfinite(a)
    t = np.where(np.isfinite(a),
                 np.clip((a - vmin) / (vmax - vmin + 1e-9), 0.0, 1.0), 0.0)
    pos = t * (stops - 1)
    i0 = np.floor(pos).astype(int)
    i0 = np.clip(i0, 0, stops - 2)
    frac = pos - i0
    for k in range(3):
        c0 = np.array([s[k] for s in rgb_stops], dtype=np.float32)
        c1 = np.array([rgb_stops[min(j + 1, stops - 1)][k] for j in range(stops)],
                      dtype=np.float32)
        out[..., k] = c0[i0] + (c1[i0] - c0[i0]) * frac
    out = np.clip(out, 0, 255).astype(np.uint8)
    out[invalid] = (0, 0, 0)
    return out
