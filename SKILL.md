---
name: cn-climate-viz
version: "1.6.1"
display_name: "中国气象要素可视化"
display_name_en: "CN Climate Multi-Variable Visualizer"
description_zh: >-
  输入一个省/市/区县（或 adcode），产出该区域任意气象要素（气温/降水/风/湿度/气压/辐射/积雪/云量/能见度）的
  GeoTIFF、科研级 HTML 预览与科研级静态出图 PNG。
  数据源三选一：Open-Meteo Archive（默认，免 Key）、NASA POWER/MERRA-2（--source nasa-power，免 Key，
  Open-Meteo 限流时的首选备选）、或自备要素 NetCDF（--nc）。
  支持时间段（单日 / 月 / 年 / 任意 start~end 区间）并按聚合方式（均值/求和/极值）累计；
  自动按行政边界裁切（GCJ-02->WGS84 校正），要素自适应 ColorBrewer 配色、连续渐变图例、指北针与图形比例尺。
  输出坐标系固定 WGS84（EPSG:4326）。
description_en: >-
  Input a Chinese province/city/district (or adcode) and produce a GeoTIFF, a scientific HTML preview and a
  publication-grade static PNG for ANY climate variable (temperature / precipitation / wind / humidity / pressure /
  radiation / snowfall / cloud / visibility). Three interchangeable data sources: Open-Meteo Archive (default,
  no key), NASA POWER/MERRA-2 (--source nasa-power, no key, recommended fallback when Open-Meteo rate-limits),
  or your own variable NetCDF (--nc). Supports time periods (single day / month / year / arbitrary start~end range)
  aggregated by mean/sum/min/max. Auto-clips to admin boundary (GCJ-02->WGS84), variable-aware ColorBrewer colormap,
  gradient legend, north arrow and scale bar. Always EPSG:4326.
agent_created: true
argument-hint: "地区 + 类型 + 时间，例：河南省 降水 2023年6-8月。\n【类型 12 选 1】气温 / 最高气温 / 最低气温 / 降水量 / 降雪量 / 风速 / 最大风速 / 相对湿度 / 气压 / 短波辐射 / 云量 / 能见度。\n【时间】单日(2023-07-15) / 某月(2023年7月) / 全年(2023年) / 起止区间(2023-06-01~2023-08-31)。\n【可选】数据源 open-meteo(默认) 或 nasa-power(限流时用)；也可直接给我一份 NetCDF。\n三项缺任意一项，我会逐项问你。"
---

# 中国气象要素可视化（CN Climate Multi-Variable Visualizer）`v1.6.1`

> **坐标系统一为 WGS84 地理坐标（EPSG:4326）**。所有产物一律是经纬度格网。

本技能是 `cn-temp-viz` 的多要素泛化版：不再局限于气温，而是把「变量」做成了可插拔预设，
并新增**时间段**支持。一个命令即可把任意气象要素按行政区渲染成科研级地图。

## 支持的要素（--preset）

| preset | 要素 | Open-Meteo 变量 | 单位 | 默认聚合 |
|---|---|---|---|---|
| `temperature` | 2m 气温 | temperature_2m_mean | °C | 均值 |
| `temperature_max` | 2m 最高气温 | temperature_2m_max | °C | 均值 |
| `temperature_min` | 2m 最低气温 | temperature_2m_min | °C | 均值 |
| `precipitation` | 降水量 | precipitation_sum | mm | 求和 |
| `snowfall` | 降雪量 | snowfall_sum | cm | 求和 |
| `wind` | 10m 风速 | wind_speed_10m_mean | m/s | 均值 |
| `wind_max` | 10m 最大风速 | wind_speed_10m_max | m/s | 均值 |
| `humidity` | 2m 相对湿度 | relative_humidity_2m_mean | % | 均值 |
| `pressure` | 海平面气压 | pressure_msl_mean | hPa | 均值 |
| `radiation` | 短波辐射 | shortwave_radiation_sum | MJ/m² | 求和 |
| `cloud` | 云量 | cloud_cover_mean | % | 均值 |
| `visibility` | 能见度 | visibility_mean | km | 均值 |

> 也可绕过预设直接 `--var <Open-Meteo daily 变量名>`（如 `temperature_2m_max`，
> 程序会反查预设并自动套用配色/单位/聚合；未知变量回退 Viridis 配色）。

## 用户会怎么提需求（先看这里）

| 用户说法 | 怎么跑 |
|---|---|
| 「安徽省气温图」 | `cn_climate.py 安徽省 out`（默认 temperature，2023 年均值） |
| 「合肥市 7 月降水」 | `cn_climate.py 合肥市 out --preset precipitation --month 7 --year 2023` |
| 「今年夏天（6-8 月）合肥累计降水」 | `cn_climate.py 合肥市 out --preset precipitation --start 2023-06-01 --end 2023-08-31` |
| 「某天风速」 | `cn_climate.py 合肥市 out --preset wind --date 2023-07-15` |
| 「用我这份 nc 出相对湿度」 | `cn_climate.py 合肥市 out --nc my.nc --var relative_humidity_2m_mean` |
| 「Open-Meteo 报 429 了 / 换个源」 | `cn_climate.py 合肥市 out --preset wind --month 6 --year 2026 --source nasa-power` |
| 「除了网页还要一张能贴报告的图」 | 默认就会额外产出 `<名>_<preset>_figure.png`；只要图不要网页则加 `--no-graticule` 精简，或手工只调 `make_figure.py` |
| 「朝阳区降水」（同名多地） | 先 `--parent` 消歧：`resolve_boundary.py 朝阳区 --list`，再 `cn_climate.py 朝阳区 out --preset precipitation --parent 长春市` |

**用户没说清范围/要素时不要直接报错** —— 用一句话问清（哪个省/市/区县，哪个要素，或给 NetCDF 文件），并附示例。

## 交互式参数收集（缺参数时主动追问）

技能被选中后，AI 应**先从用户的自然语言里抽取三个维度**：①地区（省/市/区县名或 adcode）、
②要素类型（--preset，见上表）、③时间段（单日/月/年/起止区间）。**任一缺失就用
`AskUserQuestion` 以选项卡追问，不要把缺省静默套用后直接跑**（除非用户明确给了"默认"）。**

推荐在一次 `AskUserQuestion` 里把缺失维度合并提问（最多 3 个问题）：

- **地区**（header=`地区`）：选项给 省份级（如 河南省）、城市级（如 合肥市）、区县级（如 瑶海区）；
  并提示可填「区划名 或 6 位 adcode」，同名多地需附 `--parent`。
- **类型**（header=`要素`）：选项列常用预设 ——
  `气温`(temperature) / `最高温`(temperature_max) / `最低温`(temperature_min) /
  `降水`(precipitation) / `降雪`(snowfall) / `风速`(wind) / `湿度`(humidity) /
  `气压`(pressure) / `辐射`(radiation) / `云量`(cloud) / `能见度`(visibility)。
- **时间**（header=`时间`）：选项给 `单日`(--date) / `某月`(--month+--year) /
  `某年`(--year，默认 2023) / `起止区间`(--start~--end)；并提示区间示例「2023-06-01~2023-08-31」。

追问示例话术：「要出哪里的什么要素、哪段时间？三项缺的我帮你列出来选。」

> 注：已自备 NetCDF 时，类型与时间由文件本身决定，只需追问/确认**地区**（用于裁切边界），
> 并提示用 `--nc file.nc [--var tas]`。

## 数据源

**三选一**（`--source`，默认 `open-meteo`；或 `--nc` 直接喂自己的 NetCDF）：

### 1. Open-Meteo Archive（默认，免 Key）
https://archive-api.open-meteo.com/v1/archive
GET 单请求、无需注册。变量由预设映射到 `daily` 字段（见上表）。
- 年/范围：按自然月分块 GET 后按聚合方式累计（均值/求和/极值）。
- 月/日：单月（或单日），可高分辨率。
- ⚠️ **免费档有 IP 级日配额**，超额返回 `429 Daily API request limit exceeded`（降并发/加退避都救不回来，需等 UTC 0 点重置）。遇到此错直接改用 `--source nasa-power`。

### 2. NASA POWER / MERRA-2（免 Key，`--source nasa-power`）
https://power.larc.nasa.gov/api/temporal/daily/point
逐点 GET、免 Key，是 **Open-Meteo 限流时的首选备选**（也是自建 ERA5 之外的再分析来源）。
- 预设 → POWER 参数映射写在 `variables.py` 的 `power_var`：
  气温 `T2M` / 最高 `T2M_MAX` / 最低 `T2M_MIN` / 降水 `PRECTOT` / 风速 `WS10M` / 最大风速 `WS10M_MAX`
  / 湿度 `RH2M` / 气压 `PS`(kPa，×10 转 hPa) / 辐射 `ALLSKY_SFC_SW_DWN`。
  **降雪/云量/能见度 POWER 无对应参量**（`power_var` 留空），需改用 open-meteo 或自备 NC。
- 周期取逐日值后本地按 agg 聚合；网格默认 0.1° 密集采样（MERRA-2 原生 0.5°，密集采样只为获得更平滑的市级尺度结果，**不代表 4 km 精细风场**）。
- 网络纪律同 Open-Meteo：429 长等（尊重 Retry-After）、5xx 短等、单点失败不致命（最近邻补齐）。

### 3. 自备 NetCDF（`--nc file.nc`）
走 `read_nc.py`，通用启发式读取：自动识别经纬度与要素变量、处理时间层（`--band` / `--date`）、
scale/add_offset 解包、`_FillValue`->NaN、纬度翻转、0–360 经度卷绕。

## 产物

`<名>_<preset>.tif` —— 成品要素栅格（float32，NaN nodata，EPSG:4326，已按行政边界裁切）
`<名>_<preset>.html` —— 交互式预览（Leaflet + 配色栅格 + 省界 + 图例 + 指北针 + 比例尺，底图高德/CartoDB）
`<名>_<preset>_figure.png` —— **科研级静态出图**（matplotlib；填色严格贴合行政边界 + 经纬网 + 罗盘指北针 + km 比例尺 + 色条 + 极简标题；离线自包含，适合论文/报告插图）
`<名>_<preset>_preview.png` —— 配色栅格（HTML 叠加层）
`<名>.json` —— 行政边界（DataV，GCJ-02）
`<名>_raw.tif` —— 中间文件（bbox 内规则栅格，可删）

## 常用选项

- 要素：`--preset temperature`（默认）`--preset precipitation` ...（见上表）；或直接 `--var <Open-Meteo变量>`
- 周期：`--year 2023` `--month 7` `--date 2023-07-15` `--start 2023-06-01 --end 2023-08-31`
- 聚合：`--agg mean|sum|min|max`（覆盖预设默认值）
- 网格：`--res 0.1`（强制间距，度）；年/范围模式自动限密度以控请求数
- 边界外扩：`--buffer 0.1`（**度**，fetch 用显式外扩）/ clip 的 `--buffer 500`（**米**，按距离外扩）
- **取数范围外扩（v1.5.0）：`pad_cells`（像元数，默认 3）**
  - 取数 bbox 会按 `pad_cells × 像元间距` 外扩，保证**数据范围大于行政边界**。
    这是"边界未填满"的根因所在：若取数 bbox 与边界 bbox 重合（旧行为），
    边界最外沿像元只被部分覆盖，上采样后填色到不了矢量边界，图上就是矩形缺口。
  - `clip_by_boundary.py --pad <像元数>`（默认 3）控制裁剪窗口外扩；设 `--pad 0` 可关闭。
  - 两者独立：fetch 决定**取到的数据有多大**，clip 决定**保留多大窗口**。要彻底消除缺口两者都要 > 0。
- 读 NetCDF：`--nc file.nc [--var tas] [--band 1] [--date 2023-07-15] [--bbox minx,miny,maxx,maxy] [--no-flip]`
- 同名区划消歧：`--parent 长春市`
- 数据源：`--source open-meteo`（默认）`--source nasa-power`（免 Key 备选，见上）
- 预览底：`--bg white`（默认透明）
- 底图：`--basemap amap`（默认，高德地图 GCJ-02）`--basemap carto`（CartoDB 浅色，WGS84）；
  高德模式下要素叠加层与省界自动从 WGS84 转 GCJ-02 与瓦片对齐
- 静态出图：`--dpi 300`（默认 300）`--width-in 8`（默认 8 英寸宽）`--no-graticule`（关经纬网）
  `--no-figure`（**不**生成 figure.png，只出 HTML）

## 实现要点 / 注意事项

- **网络**：本机网关对 POST 拦截、对超大响应 504。故全程 GET；Open-Meteo 多点坐标按
  CHUNK（默认 60 点/批）并发 GET，规避 414（URL 过长）并控制单响应体积；月份分块进一步压低响应。
- **坐标**：DataV 边界为 GCJ-02，要素/影像为 WGS84；裁切前边界已反解到 WGS84，避免偏移与黑边。
- **配色**：按要素自适应 ColorBrewer，且由编排器把 `--cmap-type`（diverging/sequential）与
  `--center` 透传给 `make_html.py` / `make_figure.py`：均值气温/气压用发散对称区间（气温以 0 为中心），
  最高/最低气温改用顺序色带（YlOrRd / PuBu，避免被 0 截断浪费半幅色阶）。
  **零锚定（`zero_anchored`）**：降水/辐射/降雪真从 0 起；风/湿度/云量/能见度不从 0 起，
  其图例区间**居中于数据**而非拉伸归零（如风速 2.7~3.4 → [2.5,3.5] 而非 [0,5]），避免窄区间对比度塌缩。
  - **发散色带的自适应性**：`nice_range` 在数据**不跨越** `center` 时自动退回「居中对称漂亮区间」
    （跨零数据如冬季 -5~3 °C 仍走零中心对称）。注意：**v1.4.0 起气温已改为顺序色带**
    （见下），本兜底主要用于用户显式指定 `--cmap-type diverging --center X` 的场景；
  - **气温用顺序色带而非发散色带**（v1.4.0）：气温是**绝对量**，0 °C 中心没有物理意义，
    发散色带只适用于「距平/距 0 偏差」。夏季数据全在 0 °C 以上，用发散色带会退化成单一红橙。
    预设改为 `YlOrRd_t`（浅黄→深红，"越热越深"）+ `sequential` + `center=None`；
  - **色条刻度按步长取整**：不直接用 `linspace(vmin, vmax, 5)` 当刻度（会得到 25.404 这类 3 位小数），
    按步长推算小数位并 `round` 后再设，端点仍精确等于 `vmin/vmax`。
- **科研级静态出图（`make_figure.py`）**：matplotlib Agg，**完全离线自包含**（不依赖瓦片网络）。
  - **填色管线（v1.6.0 简化，先平滑再交给 imshow 插值）**：
    1. 在**原分辨率**做掩膜加权平滑（σ=0.7 像元）；
    2. 插值交给 `imshow(bilinear)`，无效处保持 NaN（matplotlib 自动透明）；
    3. 裁剪完全交给 `clip_path`，让矢量边界成为唯一的"裁剪者"。
    **不传逐像元 `alpha`，也不手工 `zoom` 上采样。**

    > **为什么逐像元 alpha + `bilinear` 不行**：alpha 会与图像一起被插值，
    > 紧邻 NaN 的有效像元其 alpha 从 1.0 渐变到 0，形成一圈贴在行政边界内侧的
    > 半透明白边 —— 视觉上就是"边界没填满"。
    >
    > **v1.6.0 新增实测（更关键）**：`zoom(order=1)` **本身比不补洞还差**。
    > 它把上采样前的"无效置 0"线性插值成极浅色（近白），所以缺口占比
    > **2.282%**，而"什么都不做"只有 0.590%，**差 4 倍**。
    > 现行方案 0.244%，且预处理从 0.515 s 降到 0.040 s。
  - **边界内补洞（v1.6.0 修复死代码）**：上游 `all_touched` 掩膜会在边界内留下少量
    无效像元，用最近邻（迭代 4 邻域膨胀）补齐，使填色严格由矢量边界决定。

    > **这段逻辑在 v1.4.0~v1.5.0 从未执行过**：它读 `disp` 时该变量尚未定义，
    > `NameError` 被 `except Exception: pass` 静默吞掉（从未打印过"边缘补齐"）。
    > v1.6.0 修好；生效/失败都会打印，另可用 `--dump-filled <out.tif>`
    > 导出补洞后栅格核对 —— **任何静默 except 的兜底分支都必须可观测**。
  - **零 scipy（v1.6.0）+ 性能实测**：`from scipy import ndimage` 边际导入耗时
    **1067 ms**（裸启动基线 741 ms），却只用于 EDT 补洞 / `zoom` / `gaussian_filter`
    三件事，全部可改为纯 numpy。生产脚本现已 **0 处依赖 scipy**。
    结论：出图 3.4 s ≈ 启动 741 ms + 导入 1936 ms + 真干活 <0.7 s，
    **瓶颈全在进程启动与模块导入**；优化方向是"少导入、重叠等待"而非优化计算。
  - **区分「数据空洞」与「渲染白边」**（诊断要点）：先用 `geometry_mask` 数边界内 NaN。
    注意边界 geojson 通常是 **FeatureCollection 含多个要素**，
    只取 `features[0]` 会得出完全错误的结论（实测 15 vs 686 像元）。
  - 填色用 `set_clip_path` + 手工 compound path **严格贴合行政边界**（不是停在栅格像元阶梯上；
    手工构造而非 `make_compound_path_from_polys`，后者要求各环顶点数相同、遇不等长直接报错）；
  - **平滑必须用掩膜加权**：`gaussian_filter` 直接作用于含 NaN 数组会让 NaN 沿邻域扩散污染整幅栅格
    （实测 3×3 仅 1 个 NaN 即全变 NaN），须对「数值项/权重项」分别滤波后相除；
  - 坐标轴范围显式 `set_xlim/set_ylim` 钉死，避免比例尺等注记把范围撑爆、压扁地图；
    并在此基础上**向外加 6% 跨度的留白**（x/y 同比），避免行政边界紧贴图框（v1.5.0）；
  - 比例尺长度经**双重单位换算**校验并钳制在图宽 45% 内（`nice_deg` 返回值已是公里，不可再乘 1000）；
  - **指北针（v1.5.0 重做）**：克制科研风 —— 半透明白底盘 + 细外环 + 更细内环 +
    八方位刻度（正方位深墨较长、斜方位浅灰较短）+ 单色深墨细长北针（白描边）
    + 空心南针（白底细边，与北针形成轻重对比）+ 中心小点，"N" 置于环外正上方。
    **不用红/蓝实心色块**——在彩色填色上突兀且廉价；
  - 经纬网 + 经纬度刻度、5 段刻度色条、极简标题（仅区划名）、小字脚注（周期+数据源）。
- **编排器并行（v1.6.0）**：`cn_climate.py` 的 HTML 预览与静态出图互不依赖
  （都只读裁剪后的 TIF），用 `ThreadPoolExecutor` 并发执行；子进程 stdout/stderr
  按**提交顺序回放**，避免多进程输出交错成乱码。
- **健壮性**：`read_nc.py` 支持 2D 直接读取与 3D 时间轴（`--band`/`--date` 选层），
  处理 `_FillValue` 与 `valid_range/valid_min/valid_max` 无效值标记、纬度翻转、0–360 经度卷绕；
  维度过低/含垂直层会响亮报错而非静默错读；全为无效值时报错退出。
- **回归测试**：`python scripts/selftest.py` 不联网，用合成边界 + NetCDF 跑通全链路，**11 项**：
  2D 读取 / 3D 时间轴 / 发散配色 / 顺序配色 / 边界裁剪 / 静态出图 / POWER 映射 /
  色阶对比度 / NaN 安全 / **边界内补洞生效** / **numpy 原语自检**。
- **写断言的纪律（v1.6.0 血泪总结，均为实测踩坑）**：
  1. **判据必须可观测**。旧测试 10 想在 `figure.png` 上量"边界内侧白边"，
     但 `clip_path` 已把填色裁到矢量边界，该信号**恒为 0** —— 注入 bug 后仍 PASS，
     三层 RGB 欧氏距离全为 0.0，是个永远绿灯的失效判据。已废弃。
  2. **不要用「近白像素数」判断白边** —— 浅色带（如 `YlOrRd` 浅端 `#fff7ec`）
     本身就是近白，颜色法永远误报（实测占比 74% 却无 bug）。
  3. **不要用栅格索引去索引渲染后的 PNG** —— 出图经过上采样 + `bbox_inches="tight"`，
     栅格行列号与图像像素毫无对应关系，实测采样区全落在画布白边（亮度 255.0），
     断言**静默空转**、注入 bug 也照样 PASS。
  4. **要测就测逻辑的唯一产出**。新测试 10 改为用 `--dump-filled` 导出补洞后栅格，
     逐像元比对（洞必须补上、且不得越界填到边界外），这才是可观测的判据。
  5. **测试输入要能暴露破坏**。常量场会掩盖 NaN 扩散（edge 外推照样给出有限值），
     必须用**梯度场**（实测有效值 884 → 800 才暴露）。
  6. **失效的测试比没有测试更危险**，因此每次改判据都要做**负向验证**：
     临时注入 bug，确认它真的会 FAIL。测试通过 ≠ 测试有效 ——
     只有能抓住 bug 的测试才算数。v1.6.0 的两个新判据均经此验证（A/B 两个注入都被捕获）。
- **依赖**：Python 需装 rasterio / numpy / requests / shapely / Pillow / pyproj / netCDF4 / **matplotlib**
  （见 requirements.txt；matplotlib 仅静态出图需要，HTML 预览不依赖）。
  **scipy 自 v1.6.0 起不再是运行依赖**（生产脚本 0 处引用），仅 `selftest.py` 做对照验证时用。
  HTML 预览依赖 Leaflet（已内置 assets/）与本地 PNG。
- **新增要素**：在 `scripts/variables.py` 的 `VARIABLES` 里加一项即可（变量名/单位/聚合/调色板/类型；
  若该要素 NASA POWER 也有对应参量，顺带填 `power_var` / `power_units` / `power_factor`）。

## 目录结构

```
cn-climate-viz/
  SKILL.md  README.md  requirements.txt  _user_meta.json  CHANGELOG.md
  scripts/
    cn_climate.py       编排：区划名 -> 边界 -> 要素栅格 -> 裁切 -> HTML -> 静态出图
    resolve_boundary.py 区划名/adcode -> DataV 边界 GeoJSON（GCJ-02）
    fetch_climate.py    Open-Meteo 多变量多时段 GET -> 要素规则栅格 TIF
    fetch_power.py      NASA POWER(MERRA-2) 逐点密集采样 -> 要素规则栅格 TIF
    read_nc.py          通用要素 NetCDF -> TIF（2D/3D 时间轴、valid_range 掩膜）
    clip_by_boundary.py 按边界掩膜裁切（GCJ-02->WGS84，EPSG:4326）
    make_html.py        要素 TIF -> 配色 PNG + Leaflet HTML 预览（变量感知）
    make_figure.py      要素 TIF -> 科研级静态 PNG（matplotlib：边界裁剪/经纬网/指北针/比例尺/色条）
    variables.py        多要素预设注册表 + 通用科研配色/区间 + POWER 参数映射
    geo_utils.py        GCJ-02 转换 / 边界读取 / fill_nearest_numpy（最近邻补洞，fetch 与出图共用）
    selftest.py         离线回归测试（不联网，合成数据跑通全链路）
    _adcode_cache.json  区划索引缓存
  assets/               leaflet.css / leaflet.js（内置，预览自包含）
```
