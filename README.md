# 中国气象要素可视化（cn-climate-viz）`v1.6.1`

输入一个省 / 市 / 区县（或 adcode），产出该区域**任意气象要素**的 **GeoTIFF**、**交互式 HTML 预览**
与**科研级静态出图 PNG**。
支持三个可互换数据源（Open-Meteo / NASA POWER / 自备 NetCDF），全部免 Key 或自带文件。

`cn-climate-viz` 是 `cn-temp-viz` 的多要素泛化版：不再局限于气温，并把「时间段」作为一等公民。

## 支持的要素（--preset）

| preset | 要素 | 单位 | 默认聚合 |
|---|---|---|---|
| `temperature` | 2m 气温 | °C | 均值 |
| `temperature_max` | 2m 最高气温 | °C | 均值 |
| `temperature_min` | 2m 最低气温 | °C | 均值 |
| `precipitation` | 降水量 | mm | 求和 |
| `snowfall` | 降雪量 | cm | 求和 |
| `wind` | 10m 风速 | m/s | 均值 |
| `wind_max` | 10m 最大风速 | m/s | 均值 |
| `humidity` | 2m 相对湿度 | % | 均值 |
| `pressure` | 海平面气压 | hPa | 均值 |
| `radiation` | 短波辐射 | MJ/m² | 求和 |
| `cloud` | 云量 | % | 均值 |
| `visibility` | 能见度 | km | 均值 |

也可绕过预设直接 `--var <Open-Meteo daily 变量名>`，程序自动反查配色 / 单位 / 聚合。

## 快速开始

```bash
# 1) 准备 Python 环境
pip install -r requirements.txt

# 2) 安徽省 2023 年平均气温（默认年均值，网格较粗以保证请求可控）
python scripts/cn_climate.py 安徽省 out

# 3) 合肥市 2023 年 7 月降水量（单月，可高分辨率）
python scripts/cn_climate.py 合肥市 out --preset precipitation --month 7 --year 2023

# 4) 合肥市 2023 年夏季（6-8 月）累计降水 —— 时间段聚合
python scripts/cn_climate.py 合肥市 out --preset precipitation --start 2023-06-01 --end 2023-08-31

# 5) 合肥市某日风速
python scripts/cn_climate.py 合肥市 out --preset wind --date 2023-07-15

# 6) 用自备的要素 NetCDF（自动识别变量与时间层）
python scripts/cn_climate.py 合肥市 out --nc my_climate.nc --var tas --band 1

# 7) Open-Meteo 限流(429)时改用 NASA POWER / MERRA-2
python scripts/cn_climate.py 合肥市 out --preset wind --month 6 --year 2026 --source nasa-power
```

## 产物

| 文件 | 说明 |
|---|---|
| `<名>_<preset>.tif` | 成品要素栅格：float32，NaN nodata，**EPSG:4326**，已按行政边界裁切 |
| `<名>_<preset>.html` | 交互式预览：Leaflet 地图 + 配色栅格 + 省界 + 渐变图例 + 指北针 + 比例尺 |
| `<名>_<preset>_figure.png` | **科研级静态出图**（默认 300 dpi）：填色严格贴合行政边界 + 经纬网 + 罗盘指北针 + km 图形比例尺 + 色条 + 极简标题；离线自包含，适合论文/报告插图 |
| `<名>_<preset>_preview.png` | 配色栅格（HTML 的叠加层） |
| `<名>.json` | 行政边界 GeoJSON（DataV，GCJ-02） |
| `<名>_raw.tif` | 中间文件（bbox 内规则栅格，可删） |

## 数据源（三选一）

- **默认 Open-Meteo Archive**（免 Key，GET 单请求）：
  [Open-Meteo Archive API](https://open-meteo.com/en/docs/history-weather-api)
  年 / 范围模式按自然月分块 GET 后按聚合方式（均值 / 求和 / 极值）累计；月 / 日模式分辨率更高。
  ⚠️ 免费档有 **IP 级日配额**，超额即 `429 Daily API request limit exceeded`（降并发/加退避无效，需等 UTC 0 点重置）。
- **NASA POWER / MERRA-2**（`--source nasa-power`，免 Key）：
  [NASA POWER](https://power.larc.nasa.gov) 逐点接口，**Open-Meteo 限流时的首选备选**。
  预设映射：气温 `T2M`／最高 `T2M_MAX`／最低 `T2M_MIN`／降水 `PRECTOT`／风速 `WS10M`／最大风速 `WS10M_MAX`
  ／湿度 `RH2M`／气压 `PS`(kPa，×10 转 hPa)／辐射 `ALLSKY_SFC_SW_DWN`。
  **降雪 / 云量 / 能见度 POWER 无对应参量**，需改用 open-meteo 或自备 NC。
  MERRA-2 原生 0.5°，本技能按 0.1° 密集采样聚合，属**区域尺度梯度**，不代表 4 km 精细风场。
- **自备 NetCDF**：`--nc file.nc` 走 `read_nc.py`，通用启发式读取：
  自动识别经纬度与要素变量、处理时间轴（`--band` / `--date`）、`scale_factor`/`add_offset`
  解包、`_FillValue`/`missing_value` → NaN、纬度降序翻转（north-up）、经度 0–360 卷绕到 -180–180。

## 选项

| 选项 | 说明 |
|---|---|
| `--preset temperature` | 要素预设（默认 temperature；见上表） |
| `--var temperature_2m_max` | 直接传 Open-Meteo daily 变量名（覆盖预设） |
| `--year 2023` | 年份（默认 2023） |
| `--month 7` | 月份，输出该月聚合值 |
| `--date 2023-07-15` | 单日值 |
| `--start 2023-06-01 --end 2023-08-31` | 任意时间段，按聚合方式累计 |
| `--agg mean\|sum\|min\|max` | 聚合方式（覆盖预设默认） |
| `--res 0.1` | 强制网格间距（度） |
| `--nc file.nc` | 直接读入要素 NetCDF（替代在线抓取） |
| `--source open-meteo\|nasa-power` | 数据源（默认 open-meteo；nasa-power = NASA POWER/MERRA-2 免 Key 备选） |
| `--dpi 300` / `--width-in 8` | 静态出图分辨率 / 幅宽（英寸） |
| `--no-graticule` | 静态出图关闭经纬网 |
| `--no-figure` | 不生成 `figure.png`（只要 GeoTIFF + HTML） |
| `--parent 长春市` | 同名区划消歧 |
| `--buffer 0.1` | fetch 阶段边界外扩（度） |
| `--bg white` | 预览底白底（默认透明） |
| `--basemap amap` | 预览底图用高德地图（默认，GCJ-02；叠加层自动转 GCJ-02 对齐） |
| `--basemap carto` | 预览底图用 CartoDB 浅色（WGS84） |

## 实现要点

- **坐标系**：全程 WGS84（EPSG:4326）。DataV 边界为 GCJ-02，裁切前反解到 WGS84，避免偏移与黑边。
- **网络适配**：本机网关对 POST 拦截、对大响应 504，故全程 GET；Open-Meteo 多点坐标按批（默认 60 点/批）
  并发 GET，规避 414 并控制单响应体积；月份分块进一步压低响应。
- **配色（变量感知）**：编排器把 `--cmap-type`、`--center`、`--zero-anchored` 透传给渲染层。
  **气温用顺序色带 `YlOrRd_t`**（浅黄→深红，"越热越深"）—— 气温是**绝对量**，
  0 °C 中心没有物理意义，发散色带只适用于距平/偏差类数据；夏季数据全在 0 °C 以上，
  用发散色带会退化成单一红橙。海平面气压仍用发散 `Spectral`（center 取数据中点）。
  最高气温 `YlOrRd_t`、最低气温 `PuBu`（顺序，避免被 0 截断浪费半幅色阶）。
  **零锚定区分**：降水 / 辐射 / 降雪真从 0 起；风 / 湿度 / 云量 / 能见度不从 0 起，
  其图例区间**居中于数据**（风速 2.7~3.4 → `[2.5, 3.5]` 而非 `[0, 5]`），避免窄区间对比度塌缩。
  发散色带在**数据不跨越 center** 时自动退回居中区间（用户显式指定 diverging 时的兜底）。
  色条刻度按步长取整，不出现 `25.404` 这类 3 位小数标签。
- **HTML 预览**：依赖内置 Leaflet（assets/）与本地 `*.png` 叠加层，完全自包含（仅底图瓦片走网络）。
  注记遵循科研出图规范：极简标题（仅区划名 + 周期/要素，剔除 zoom/源/时间副标题）、
  罗盘式指北针、线状色块图例（行政边界）、图形比例尺。
- **科研级静态出图（`make_figure.py`）**：matplotlib Agg，**完全离线**（不依赖瓦片）。
  - **填色管线（v1.6.0 简化）**：① 原分辨率做**掩膜加权平滑**（σ=0.7 像元）；
    ② 插值交给 `imshow(bilinear)`，无效处保持 NaN（matplotlib 自动透明）；
    ③ 裁剪完全交给 `clip_path`（矢量边界是**唯一**的裁剪者）。
    不再手工 `zoom` 上采样，也不给 `imshow` 传逐像元 `alpha`。
    > 为什么：逐像元 alpha 若与 `bilinear` 一起插值，紧邻 NaN 的有效像元会形成半透明白边。
    > 而 v1.6.0 实测发现 **`zoom(order=1)` 本身更糟**：它把上采样前的"无效置 0"
    > 线性插值成极浅色（近白），缺口占比 2.282%，**比不补洞（0.590%）还差 4 倍**。
    > 现方案 0.244%，且预处理从 0.515 s 降到 0.040 s。
  - **边界内补洞（v1.6.0 修复）**：上游 `all_touched` 掩膜会在边界内留下少量无效像元，
    用最近邻（迭代 4 邻域膨胀）补齐，使填色严格由矢量边界决定。
    > **v1.4.0~v1.5.0 这段逻辑从未执行过**：它读 `disp` 时变量尚未定义，
    > `NameError` 被 `except Exception: pass` 静默吞掉。v1.6.0 已修，
    > 且生效/失败都会打印，可用 `--dump-filled <out.tif>` 导出补洞后栅格核对。
  - **数据范围外扩（v1.5.0 根治）**：旧版取数 bbox 与边界 bbox **几乎完全重合**
    （实测 `116.6824~117.9680` vs 边界 `116.6876~117.9733`），边界最外沿像元只被
    部分覆盖 → 上采样后填色到不了矢量边界，形成矩形缺口。
    现 `build_grid(pad_cells=3)` 让取数范围比边界大出约 3 个像元，
    裁剪窗口同步 `--pad 3`，数据真正环绕边界一圈。
  - **零 scipy（v1.6.0）**：三处 scipy 用法（EDT 补洞 / `zoom` / `gaussian_filter`）全部
    改为纯 numpy 实现。理由：`from scipy import ndimage` 边际导入耗时 **1067 ms**，
    占本脚本总耗时约 1/3，而它只干这三件事。生产脚本现已 **0 处依赖 scipy**。
  - 填色用 `set_clip_path` + 手工 compound path 严格贴合行政边界；
  - 坐标轴范围显式钉死并向外加 **6% 留白**（v1.5.0，边界不再贴图框）；
    比例尺长度经单位换算校验并钳制在图宽 45% 内；
  - **指北针（v1.5.0 重做）**：半透明白底盘 + 细外环/内环 + 八方位刻度 +
    单色深墨细长北针 + 空心南针 + 中心点，"N" 在环外正上方；不用红蓝实心色块；
  - 含经纬网/刻度、5 段刻度色条、极简标题与脚注（周期 + 数据源）。
- **性能（v1.6.0 实测）**：
  - 微基准（裸解释器启动 **741 ms** 为基线）各依赖**边际**导入：
    `matplotlib.pyplot` 1145 ms、**`scipy.ndimage` 1067 ms**、`rasterio` 710 ms、
    `matplotlib`(Agg) 644 ms、`shapely` 339 ms。
  - 结论：`make_figure.py` 3.4 s ≈ 启动 741 ms + 导入 1936 ms + 真干活 <0.7 s，
    **瓶颈全在进程启动与模块导入**。故优化方向是"少导入、重叠等待"，
    而不是优化计算（计算早已不是瓶颈）。
  - **编排器并行**：`cn_climate.py` 的 HTML 预览与静态出图互不依赖（都只读裁剪后 TIF），
    用 `ThreadPoolExecutor` 并发，子进程输出按提交顺序回放避免交错乱码。
  - **放弃方案**：`figure+canvas` 绕过 pyplot —— 实测 1.814 s vs 1.814 s，**无收益**
    （大头是 `matplotlib` 本体依赖，不是 pyplot 封装）。
- **要素可选项提示**：`cn_climate.py --help`（或无参）打印全部 **12** 个要素的名称、
  中文标签、单位、默认聚合与数据源支持情况；传入未知 `--preset` 时同样列出全表并报错退出
  （不再静默回退默认气温）。
- **NetCDF 健壮性**：`read_nc.py` 支持 2D 直接读与 3D 时间轴（`--band`/`--date` 选层），
  处理 `_FillValue` 与 `valid_range/valid_min/valid_max`、`scale_factor/add_offset`、
  纬度翻转、0–360 经度卷绕；维度异常或全为无效值时响亮报错。
- **扩展要素**：在 `scripts/variables.py` 的 `VARIABLES` 里加一项即可。

## 测试 / 回归（离线）

不联网，用合成边界 + 合成 NetCDF 跑通 `read_nc -> clip -> make_html / make_figure` 全链路，
**11 项**：2D / 3D 时间轴 / 发散配色 / 顺序配色 / 边界裁剪 / 静态出图 / POWER 映射 /
色阶对比度 / NaN 安全 / **边界内补洞生效** / **numpy 原语自检**。

```bash
python scripts/selftest.py
```

退出码 0 表示通过，1 表示失败（并会打印首个失败点）。改完脚本建议先跑一遍。

v1.6.0 重构了末尾两项，并把过程教训写进注释（**"测试通过 ≠ 测试有效"**）：

- **旧测试 10 已废弃**：它试图在 `figure.png` 上量「边界内侧白边」，但该信号
  **不可观测** —— `clip_path` 把填色严格裁到矢量边界，栅格层面的半透明/缺口整个被裁掉。
  负向验证实证：注入 alpha 插值 bug 后，核心/边缘/最边缘三层 RGB **欧氏距离全为 0.0**。
  该测试**永远是绿灯**，属失效判据。（`_preview.png` 的 alpha 中间值也恒为 0，
  因 `make_html` 用 `Image.NEAREST` 生成 alpha，本就二值。）
- **新测试 10**：直接度量补洞逻辑的**唯一产出** —— 边界内原本无效的像元经 `make_figure`
  处理后是否已赋有效值（经 `--dump-filled` 逐像元比对），并反向约束"不得越界填充"
  与"补洞值须在原值域内"。
- **新测试 11**：numpy 原语自检（核归一化 / 常量场保持 / NaN 不扩散 / 补洞值域 /
  贴边外推 / 空输入不崩）。
  > 关键坑：**必须用梯度场而非常量场**。常量场下"直接对含 NaN 数组滤波"的破坏完全
  > 不可见（实测注入 bug 后仍 PASS，有效值仍有限）；换梯度场立刻暴露（884 → 800）。
- **负向验证**（注入 bug 确认会 FAIL）：① 补洞抛异常被静默降级 → 报错
  `边界内仍有 47/47 个像元未补齐`；② 平滑改为直接滤波（NaN 扩散）→ 已捕获。

## 目录结构

```
cn-climate-viz/
  SKILL.md  README.md  requirements.txt  _user_meta.json  CHANGELOG.md
  scripts/
    cn_climate.py       编排
    resolve_boundary.py 区划名 -> 边界 GeoJSON
    fetch_climate.py    Open-Meteo -> 要素栅格（多变量/多时段）
    fetch_power.py      NASA POWER(MERRA-2) -> 要素栅格（逐点密集采样）
    read_nc.py          NetCDF -> 要素栅格
    clip_by_boundary.py 边界裁切
    make_html.py        TIF -> 配色 PNG + HTML（变量感知）
    make_figure.py      TIF -> 科研级静态 PNG（matplotlib）
    variables.py        多要素预设注册表 + 通用科研配色/区间 + POWER 参数映射
    geo_utils.py        GCJ-02 转换 / 边界读取 / fill_nearest_numpy
    selftest.py         离线回归测试（不联网，11 项）
    _adcode_cache.json  区划索引缓存
  assets/               leaflet.css / leaflet.js
```

## 依赖

`rasterio / numpy / requests / shapely / Pillow / pyproj / netCDF4 / matplotlib`
（`matplotlib` 仅静态出图需要，HTML 预览不依赖）。

> **scipy 自 v1.6.0 起不再需要**：生产脚本已 0 处引用，
> 仅离线回归测试 `selftest.py` 做对照验证时会用到（requirements.txt 中已注释掉）。
