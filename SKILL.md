---
name: edem-deck-editor
description: 通过 h5py 直接读写 EDEM 2020–2022.2 的 deck 文件（.dem + _data/0.h5 + 同名 .dfg）实现前处理与求解配置自动化——改磨机转速（rad/s 或 rpm）、颗粒数量（含 0 颗安全停用工厂）、颗粒直径/半径（自动联动重算体积/质量/转动惯量）、工厂绑定颗粒修正、材料属性、接触参数、周期边界、接触模型启停，以及 .dfg 求解器时间步、搜索网格、Track Collisions 开关，并可从 Excel 参数矩阵批量克隆多组工况。当用户提到 EDEM 前处理、改仿真参数、批量生成不同工况的 deck 或参数扫描时使用本 skill。
---

# EDEM Deck 参数编辑器（前处理 + 求解配置自动化）

> 版本 **v2.0** ｜ 实测基线 Altair EDEM 2022.1（内部标识 `EDEM_8_0_0_RC1`，HDF5 `file version = 2622234`，`.dfg` `file-version = 30.0.3`）
> 适用：EDEM 2020 – 2022.2（这些版本没有前处理写入 API，只能走本工具的文件直改路线）

## 环境准备

工具只做文件读写，不依赖 EDEM 进程；跑求解才需要 EDEM 本体。三个量需要先确定：

| 变量 | 用途 | 说明 |
|---|---|---|
| `EDEM_BIN` | 控制台求解器 `edem.exe` 全路径 | 仅批量求解时需要。默认位置形如 `<EDEM 安装目录>\EDEM\bin\edem.exe` |
| Python | 跑本工具脚本 | **3.8+**，需 `h5py`、`numpy`、`openpyxl`（仅批量脚本用） |
| deck 路径 | 待改的 `*.dem` | `.dem` 与同名 `_data/` 文件夹必须同名同目录 |

```powershell
# 建议先设成环境变量，后文命令直接引用
$env:EDEM_BIN = Join-Path $env:ProgramFiles "Altair\2022.1\EDEM\bin\edem.exe"
$env:EDEM_PY  = "python"        # 或你的 Conda / venv 解释器全路径
```

安装依赖：

```bash
pip install h5py numpy openpyxl
```

> **Windows 提示**：若裸 `python` 指向 Microsoft Store 的占位程序（会弹应用商店），改用解释器绝对路径，如 Conda 环境里的 `envs\<env>\python.exe`。

## 核心知识：EDEM 的三类关键文件

一个完整的 EDEM 仿真由三部分配合工作：

```
my_sim.dem                      ← 主索引文件（HDF5，根属性 num timesteps=1 表示干净 0 秒态）
my_sim.dfg                      ← Simulator / Analyst 纯文本配置（KEYPATHVALUE 格式，存时间步/网格/Track Collisions）
my_sim_data/                    ← 数据文件夹（必须与 .dem 同名同目录）
    ├── 0.h5                    ← CreatorData/0（全部前处理模型与参数）+ TimestepData/0（t=0 初始帧）
    └── 1.h5 ... N.h5           ← 已跑出的后续时间步结果分片
```

- **前处理参数**在 `_data/0.h5` 的 `CreatorData/0` 组；
- **Simulator 求解参数**（固定步长 `FIXED TIMESTEP`、网格尺寸 `GRID CELL SIZE`、全步长碰撞记录 `TRACK COLLISIONS`、写盘间隔 `WRITE:INTERVAL`、总时长 `WRITE:TOTAL`）以明文存在同名 **`.dfg`** 的 `PROCESSOR:*` 键中。

## 参数映射表（实测）

| 修改项 | 存储位置 | 联动保护机制 |
|---|---|---|
| **磨机转速** | `0.h5`: `GeometryGroups/{k}/Kinematics/{i}@initial angular velocity` | 支持 `--speed` (rad/s) 或 `--rpm` (r/min) + `--clockwise`（+Z 正视顺时针自动取负） |
| **颗粒半径/直径** | `0.h5`: `ParticleTypes/{i}/spheres` (`physicalRadius`, `contactRadius`) | 支持 `--radius` (m) 或 `--diameter-mm`；**自动查材料密度同步重算 `raw volume`、`raw mass`、`raw inertia` (3,) 并置 `auto properties=1`** |
| **颗粒数量** | `0.h5`: `GeometryGroups/{i}/Factories/{f}@num to create` & `@enabled` | **传 `0` 时自动设 `enabled=0` 安全停用工厂**（防报错/无限生成）；传正数设 `enabled=1, unlimited=0`；支持 `--fill-duration` 自动折算 `creation rate` |
| **工厂绑定颗粒** | `0.h5`: `Factories/{f}/FactType/type names` & `FactSize/{name}` | `--list` 自动报警多工厂绑同一颗粒；`--factory-type` 一键修正 |
| **周期性边界** | `0.h5`: `Domain@domain xPeriodic / yPeriodic / zPeriodic` (`uint8`) | `--list` 显式检查报警；`--periodic z=0` 一键关闭 |
| **材料与接触** | `0.h5`: `Materials` / `Interactions` compound dataset | 全部支持 `nargs='+'` 一次传多项，只做一次轻量备份 |
| **接触模型启停** | `0.h5`: `PhysicsModels/{cat}/order` + `@enable rolling friction` | 启用滚动摩擦时同步把组内 `enable rolling friction` 属性置 `1` |
| **求解器设置** | `.dfg`: `PROCESSOR:GENERAL:*` / `PROCESSOR:WRITE:*` | `--timestep` / `--cell-size` / `--track-collisions` / `--save-interval` / `--sim-time` / `--threads` |

## 单文件脚本用法（`scripts/edem_set_param.py`）

```powershell
$PY = $env:EDEM_PY
$SC = ".\scripts\edem_set_param.py"

# 1. 全面体检当前 deck（含文件 0 秒态、转轴方向、工厂绑定报警、周期边界、.dfg 求解设置）
& $PY $SC "./cases/mill.dem" --list

# 2. 一次性修好母版常见隐患（修工厂绑定、修衬板刚度密度、开滚动摩擦、关 Z 周期边界、开碰撞记录、重置回 0 秒）
& $PY $SC "./cases/mill.dem" `
    --reset-zero `
    --factory-type "Steel_factory=Steel_Ball" "Ceramic_factory=Ceramic_Ball" `
    --material-density "mill=7800" --material-shear "mill=7e10" --material-poisson "mill=0.3" `
    --interaction "Steel~Steel=0.5,0.5,0.01" "Steel~mill=0.5,0.5,0.01" "Ceramic~Steel=0.35,0.32,0.15" "Ceramic~mill=0.35,0.32,0.15" "Ceramic~Ceramic=0.6,0.5,0.05" `
    --model-enable "SurfaceGeometry=Standard Rolling Friction" `
    --periodic z=0 `
    --track-collisions 1 --sim-time 20 --save-interval 0.02 --threads 8

# 3. 按工程单位改单个工况（如 40 r/min 顺时针、Φ18 mm 瓷球 1240 颗、0.5 s 下完料）
& $PY $SC "./cases/case.dem" `
    --rpm 40 --clockwise `
    --diameter-mm 18 --radius-material Ceramic `
    --ball-count "Steel_factory=463" "Ceramic_factory=1240" --fill-duration 0.5 `
    --timestep 7.5e-7 --cell-size 0.0225
```

默认每次修改前自动备份到 `<deck 所在目录>/_edem_backup/<deck名>_<时间戳>/`（含 `.dem` / `0.h5` / `.dfg`）；`--no-backup` 可关闭。

## 批量派生工况（`scripts/edem_batch_from_xlsx.py`）

从一张参数矩阵 Excel 里读工况，从校准好的母版批量克隆出干净的 0 秒工况文件夹，并生成 `edem.exe -c` 排队求解脚本 `run_batch_edem.ps1`。

```powershell
$PY = $env:EDEM_PY
$BATCH = ".\scripts\edem_batch_from_xlsx.py"

# 先 dry-run 预览全部参数映射
& $PY $BATCH `
    --template "./cases/mill.dem" `
    --xlsx "./cases/cases.xlsx" `
    --out-dir "./cases/batch" `
    --clockwise --dry-run
```

Excel 的列按**表头名**识别，不看列序。必需表头（中英文别名均可）：工况编号 / `case_id`、转速 / `rpm`、钢球直径 / `steel_d_mm`、瓷球直径 / `ceramic_d_mm`、钢球数量 / `steel_count`、瓷球数量 / `ceramic_count`、时间步 / `timestep_us`；详见 `examples/cases.example.csv`。`--sheet` 指定工作表，`--data-start-row` 指定首个数据行（表头默认取它的上一行，也可用 `--header-row` 单独指定）。缺列会直接报错并打印该行识别到的表头。工厂名、材料名用 `--steel-factory` / `--ceramic-factory` / `--steel-material` / `--ceramic-material` 按母版实际情况覆盖。

## 控制台无界面求解与数据导出（`edem.exe` CLI）

无需在 GUI 里逐个点求解，可用控制台模式顺序运行（`run_batch_edem.ps1` 由批量脚本自动生成，`$EDEM_BIN` 未设置时请手工改成实际路径）：

```powershell
# 后台无界面求解（-R 归零，--particle-properties 让内核复算颗粒属性，-E 2 可切 CUDA GPU）
& $env:EDEM_BIN -c -i "case.dem" -R --particle-properties -r 20 -w 0.02 -t 7.5e-7 -g 2.5 -p 8

# 命令行导出碰撞数据 (K) 或颗粒数据 (P)
& $env:EDEM_BIN -c -i "case.dem" -e K collisions.csv
```

## 已知陷阱

1. **不要 `import edempy` 做前处理校验**：EDEM 2022.1 自带的 `edempy 0.2.0` 只有后处理只读能力，且在 `numpy 2.x` 下因 `numpy.string_` 被移除直接 `ImportError`。统一用 `h5py` 读写。
2. **Compound dtype 必须「整表读出 → 改 numpy → `dset[...] = data` 整体写回」**：切勿写 `dset[i]['field'] = v`（落在临时副本上不会落盘）。
3. **周期边界属性名带 `domain ` 前缀**：真实键名是 `domain xPeriodic / yPeriodic / zPeriodic`（`uint8`），不是裸 `zPeriodic`。
4. **单介质验证组（0 颗球）不要只填 `num to create = 0`**：正确做法是把对应工厂设 `@enabled = uint8(0)`（本工具 `--ball-count Fac=0` 已自动处理）。

## 边界

- 只处理 **单球（single-sphere）** 颗粒的半径联动；多球团簇（multi-sphere）与多面体（polyhedral）缩放需另加子球坐标等比逻辑，或求解时挂 `--particle-properties` 交内核重算。
- 运动学只匹配 `type == 'Linear Rotation'`；振动筛 `Sinusoidal Translation/Rotation`、皮带 `Conveyor`、多级耦合运动需按类型扩展。
- 扩展接触模型（JKR、Bonding V2、液桥）的专属参数存放在独立插件组，不在基础 `Interactions` 表内，暂不支持。
- `0.h5` 的 `file version` 与 `.dfg` 键路径是 Altair 内部格式而非公开契约；跨大版本写入前建议先 `--list` 核对并保留备份。
- 本工具仅读写用户本地自有仿真配置文件；运行求解仍需用户自行具备合法的 Altair EDEM 软件及许可证。
