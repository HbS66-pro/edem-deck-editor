---
name: edem-deck-editor
description: 通过 h5py 直接读写 Altair EDEM 2020–2022.2 的 deck 文件（.dem + _data/0.h5 + 同名 .dfg），实现任意 DEM 仿真场景的前处理改参、接触参数标定扫描与批量工况派生——支持旋转/平动/振动等运动学修改、单球与多球缩放（自动联动体积/质量/惯量）、按数量或质量控制工厂（0 安全停用）、材料/接触单项与批量修改、周期边界与接触模型启停、.dfg 求解配置、从任意 deck 自动导出 CSV 参数模板，以及从 CSV/XLSX 矩阵批量生成干净 0 秒工况与 PowerShell/Bash 控制台求解队列。当用户提到 EDEM 前处理、改仿真参数、接触标定、批量生成工况 deck 或参数扫描时使用本 skill。
---

# EDEM Deck 参数编辑器（通用前处理 + 批量矩阵自动化）

> 版本 **v0.1.0** ｜ 实测基线 Altair EDEM 2022.1（HDF5 `file version = 2622234`，`.dfg` `file-version = 30.0.3`）
> 适用范围：EDEM 2020 – 2022.2（无官方前处理写入 API；EDEM ≥ 2022.3 建议优先使用官方 `edempy` Deck Editing API）

## 脚本定位与环境约定（跨端通用）

- **脚本目录**：本技能同目录下的 `scripts/`（记作 `<SKILL_DIR>/scripts/`）。**调用时请基于本 `SKILL.md` 所在目录拼出脚本绝对路径**，不要假设当前工作目录在技能目录内。
- **Python 解释器**：Python 3.8+，需 `h5py`、`numpy`（仅读写 `.xlsx` 时需 `openpyxl`，`.csv` 零额外依赖）。在 Windows 上若裸 `python` 指向 Microsoft Store 占位程序，改用 Conda / venv 解释器绝对路径。
- **求解器路径（可选）**：仅执行生成的控制台排队脚本时需要 `edem.exe` / `edem`，通过环境变量 `EDEM_BIN` 传入，脚本内不硬编码安装目录。

```powershell
# 示例：将 <SKILL_DIR> 替换为本 SKILL.md 实际所在目录
$PY = "python"                                   # 或本机 Conda Python 全路径
$SET = "<SKILL_DIR>\scripts\edem_set_param.py"
$BATCH = "<SKILL_DIR>\scripts\edem_batch.py"
$env:EDEM_BIN = Join-Path $env:ProgramFiles "Altair\2022.1\EDEM\bin\edem.exe"
```

## 核心知识：EDEM 的三类文件

```
my_sim.dem                      ← 主索引文件（HDF5，num timesteps=1 表示干净 0 秒态）
my_sim.dfg                      ← Simulator / Analyst 纯文本配置（KEYPATHVALUE 格式）
my_sim_data/                    ← 数据文件夹（必须与 .dem 同名同目录）
    ├── 0.h5                    ← CreatorData/0（前处理模型参数）+ TimestepData/0（t=0 初始帧）
    └── 1.h5 ... N.h5           ← 已求解的时间步分片
```

## 标准工作流（先体检，再修改/派生）

### 第 1 步：先体检 deck（只读）

```powershell
& $PY $SET "./cases/template.dem" --list
& $PY $SET "./cases/template.dem" --list-models
```

`--list` 会汇报：`file_version` 是否在已验证基线内、是否干净 0 秒态、**全部运动学**（旋转/平动/振动等）、颗粒类型与球数、工厂模式（按数量/按质量）与绑定校验（自动报警多工厂绑同一颗粒）、材料、接触对、周期边界、接触模型顺序及 `.dfg` 求解设置。

### 第 2 步：单 deck 改参（自动备份到 `_edem_backup/`）

| 修改项 | CLI 选项 | 联动与防呆 |
|---|---|---|
| 旋转转速 | `--rpm 40 --clockwise` 或 `--speed 4.19`（多转动件加 `--geometry Name`） | 自动校验唯一性；多转动件不猜，提示指定 `--geometry` |
| 任意运动学属性 | `--kinematic-attr "Screen:amplitude=0.005" "Drum:start time=0.2"` | 保持原 HDF5 dtype，支持平动/振动/起止时间等 |
| 粒径 / 缩放 | `--diameter-mm 18` / `--radius 0.009` / `--scale 1.2`（配合 `--radius-material`） | 单球自动重算 `raw volume/mass/inertia`；多球等比缩放子球 `pos` 并提示 `--particle-properties` |
| 工厂数量 / 质量 | `--ball-count "Fac=500"` / `--factory-mass "Fac=5.0"`（配合 `--fill-duration`） | 传 `0` 自动设 `enabled=0` 安全停用；支持 `--factory-rate` 与 `--factory-type` |
| 材料属性 | `--material-density` / `--material-shear` / `--material-poisson` | **改密度时自动联动重算对应单球颗粒的 `raw mass` 和 `raw inertia`** |
| 接触参数 | `--interaction "A~B=e,sf,rf"` 或单项 `--interaction-e` / `-sf` / `-rf` | 支持 `A~B` 与 `B~A` 双向匹配，单项修改不覆盖另外两项 |
| 域、周期、重力、模型 | `--domain-min/max`、`--periodic z=0`、`--gravity 0,-9.81,0`、`--model-enable/disable` | 启停 `Standard Rolling Friction` 同步 `@enable rolling friction` 属性 |
| 求解器 `.dfg` 与归零 | `--timestep`、`--cell-size`、`--track-collisions`、`--save-interval`、`--sim-time`、`--threads`、`--reset-zero` | `--reset-zero` 清理 `1..N.h5` 并将 `num timesteps` 置 1 |

### 第 3 步：从任意 deck 导出模板并批量派生（CSV / XLSX）

1. **自动生成该 deck 专属的参数矩阵 CSV 模板**（无需手猜工厂名、颗粒名、接触对名）：

```powershell
& $PY $SET "./cases/template.dem" --export-template-csv "./cases/matrix.csv"
```

2. **填写参数矩阵（声明式列名协议，适用于任意设备与任意物种数）**：

| 列名格式 | 含义 |
|---|---|
| `case_id`（必填） | 工况文件夹与 `.dem` 名称（兼容别名 `工况编号`/`case`/`id`） |
| `rpm` 或 `rpm:<几何名>` / `speed_rad:<几何名>` | 旋转速度（r/min 或 rad/s） |
| `kin:<几何名>:<属性名>` | 任意运动学属性（如 `kin:Screen:amplitude`） |
| `d_mm:<颗粒或材料>` / `r_m:<目标>` / `scale:<目标>` | 直径 mm / 半径 m / 缩放系数（**空白或 `<=0` 安全跳过，单介质验证组不会崩**） |
| `count:<工厂名>` / `mass_kg:<工厂名>` / `rate:<工厂名>` | 工厂生成数 / 质量 kg / 速率（`0` 自动停用工厂） |
| `density:<材料>` / `shear:<材料>` / `poisson:<材料>` | 材料物性 |
| `interaction:<A~B>` 或 `e:<A~B>` / `sf:<A~B>` / `rf:<A~B>` | 接触参数（单项列特别适合休止角/剪切标定扫描） |
| `periodic_x` / `periodic_y` / `periodic_z`、`gravity` | 仅当表内有列或显式传 `--periodic` 时才改，**绝不静默篡改母版周期边界** |
| `timestep_s` / `timestep_us`、`cell_size_m`、`sim_time_s`、`save_interval_s`、`track_collisions`、`threads` | `.dfg` 求解配置（未指定项保持母版原值） |

> 同时 100% 向后兼容旧版双介质球磨机中文/英文表头（如 `目标转速`、`钢球直径`、`瓷球直径`、`钢球数量`、`瓷球数量`、`步长`）以及旧入口 `scripts/edem_batch_from_xlsx.py`。

3. **先 `--dry-run` 预览，再实跑生成工况与求解队列（支持 `.ps1` / `.sh`）**：

```powershell
# 预览解析结果
& $PY $BATCH --template "./cases/template.dem" --matrix "./cases/matrix.csv" --out-dir "./cases/batch" --dry-run

# 实际生成（Windows .ps1 + Linux .sh 可通过 --shell ps1|sh|both 控制，GPU 加 --gpu）
& $PY $BATCH --template "./cases/template.dem" --matrix "./cases/matrix.csv" --out-dir "./cases/batch" --shell both
```

## 控制台无界面求解与导出（`edem` CLI）

```powershell
& $env:EDEM_BIN -c -i "case.dem" -R --particle-properties -r 20 -w 0.02 -t 7.5e-7 -g 2.5 -p 8
& $env:EDEM_BIN -c -i "case.dem" -e K collisions.csv
```

## 已知陷阱

1. **不要 `import edempy` 做前处理校验**：EDEM 2022.1 自带的 `edempy 0.2.0` 仅后处理只读，且在 `numpy >= 2.0` 下因 `numpy.string_` 移除直接 `ImportError`。统一用 `h5py`。
2. **Compound dataset 必须整表往返**：先 `data = dset[()]`，改 numpy 数组，再 `dset[...] = data` 整体写回；`dset[i]['field'] = v` 落在临时副本上不会落盘。
3. **周期边界属性名带 `domain ` 前缀**：真实键名为 `domain xPeriodic / yPeriodic / zPeriodic`（`uint8`）。
4. **停用工厂不要只设 `num to create = 0`**：必须将 `@enabled` 设为 `uint8(0)`（本工具传 `0` 已自动处理）。

## 边界

- **多球/多面体质量惯量**：多球缩放已等比缩放子球半径与局域 `pos` 坐标，但重叠球团真实惯量张量需求解时挂 `--particle-properties` 由 EDEM 内核复算；多面体顶点网格暂未直接缩放。
- **扩展接触插件参数**：JKR、Bonding V2、液桥等专属参数存于独立插件组，不在基础 `Interactions` 表中，暂不支持。
- **内部文件格式**：HDF5 `file version` 与 `.dfg` 键是 Altair 内部格式，非公开契约。在非 2022.1 版本写入前请先 `--list` 核对并保留自动备份。
- **授权声明**：本工具仅读写用户自有本地仿真文件，求解仍需合法的 Altair EDEM 授权。
