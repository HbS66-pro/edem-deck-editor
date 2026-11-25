# edem-deck-editor

**在命令行改 Altair EDEM 仿真 deck，不点 GUI，也不需要前处理 API。**

[English →](README.md)

一个 agent skill + 两个 Python CLI，用 `h5py` 直接读写 EDEM 的 deck 文件。它出自一次真实的球磨机 DEM 参数研究：一个校准好的母版 deck，几十组变化介质尺寸、球数、转速和接触参数的工况。手工点完这个矩阵才是瓶颈，而 EDEM ≤ 2022.2 根本没有可编程的前处理接口。

## 能做什么

| 能力 | 值落在哪 | 防呆 |
|---|---|---|
| 磨机转速 | 转动几何的 `initial angular velocity` | `--speed`（rad/s）或 `--rpm` + `--clockwise` 定正负号 |
| 颗粒半径/直径 | `ParticleTypes/*/spheres` | 按材料密度重算 `raw volume`、`raw mass`、`raw inertia`，并置 `auto properties=1` |
| 颗粒数量 | `Factories/*@num to create` | 传 `0` 是设 `enabled=0`，不是留个坏工厂；`--fill-duration` 反推生成速率 |
| 工厂→颗粒绑定 | `FactType/type names`、`FactSize` | `--list` 会报"两个启用工厂绑了同一颗粒" |
| 材料与接触 | `Materials`、`Interactions` 复合表 | 一次命令可传多项，只做一次备份 |
| 周期边界 | `Domain@domain {x,y,z}Periodic` | `--list` 显示；`--periodic z=0` 关闭 |
| 接触模型启停 | `PhysicsModels/*/order` | 同时同步 `enable rolling friction` 属性 |
| 求解器设置 | `.dfg` 的 `PROCESSOR:GENERAL:*` / `PROCESSOR:WRITE:*` | 时间步、网格、Track Collisions、保存间隔、总时长、线程数 |
| 批量派生 | `cases.xlsx` → N 套干净 0 秒 deck | 顺带生成 `run_batch_edem.ps1` 排队脚本给 `edem.exe -c` |

## 为什么不用 `edempy`？

因为目标版本上它做不到。

- **EDEM ≤ 2022.2**：自带的 `edempy`（2022.1 上是 0.2.0）只有后处理只读能力——读已算完的时间步里的颗粒坐标、速度、接触力，没有创建或修改前处理模型的 API。而且它在 `numpy >= 2.0` 下导入即崩（`numpy.string_` 被移除）。
- **EDEM ≥ 2022.3**：Altair 补了 Deck Editing API，覆盖材料、接触、粒径、工厂和基础运动学。如果你在新版本上，用它——那是有官方支持的契约，本文这版是逆向出来的。
- **两头之间什么都没有。** 所以工具直接写 EDEM 自己写的文件：`_data/0.h5`（CreatorData 组）和同名 `.dfg`（`KEYPATHVALUE` 文本）。

这也决定了它的诚实边界：**这不是官方支持的格式。** 见[边界](#边界)。

## 安装

Python 3.8+。

```bash
pip install h5py numpy openpyxl
```

`openpyxl` 只有批量脚本用得上。两个 CLI 都是单文件，没别的安装步骤。

想让生成的批处理脚本自己找到 `edem.exe`，设一次环境变量：

```powershell
$env:EDEM_BIN = Join-Path $env:ProgramFiles "Altair\2022.1\EDEM\bin\edem.exe"
```

## 用法

动手前先体检——文件状态、转轴方向、工厂绑定、周期边界、接触模型和 `.dfg` 求解块：

```bash
python scripts/edem_set_param.py D:/cases/mill.dem --list
```

按工程单位改单个工况（40 r/min 顺时针、Φ18 mm 瓷球、1240 颗）：

```bash
python scripts/edem_set_param.py D:/cases/case.dem \
    --rpm 40 --clockwise \
    --diameter-mm 18 --radius-material Ceramic \
    --ball-count "Steel_factory=463" "Ceramic_factory=1240" --fill-duration 0.5 \
    --timestep 7.5e-7 --cell-size 0.0225
```

从 Excel 表派生整个矩阵（先 dry-run，它会把每条解析出来的参数打印给你看）：

```bash
python scripts/edem_batch_from_xlsx.py \
    --template D:/cases/mill.dem \
    --xlsx D:/cases/cases.xlsx \
    --out-dir D:/cases/batch \
    --clockwise --dry-run
```

去掉 `--dry-run` 才真正克隆。每个工况一个文件夹，新的 `0.h5`、`num timesteps` 重置为 1，`.dfg` 带上自己的时间步与网格。

列**按表头名识别，不看列序**——中英文别名都行（`case_id`、`rpm`、`steel_d_mm`、`ceramic_d_mm`、`steel_count`、`ceramic_count`、`timestep_us`，或 工况编号 / 目标转速 / 钢球直径 之类）。见 `examples/cases.example.csv`。`--sheet` 指到工作表，`--data-start-row` 指到首个数据行（表头默认取它的上一行，也可用 `--header-row` 单独指定）。缺列会直接报错并打印它看到的表头，而不是默默读错数字。

## 输出

- 原地修改 deck，自动备份到 `<deck 目录>/_edem_backup/<deck>_<时间戳>/`（`.dem`、`0.h5`、`.dfg`）。`--no-backup` 可关。
- 批量模式：`--out-dir/<工况>/<工况>.dem` + `<工况>_data/0.h5` + `<工况>.dfg`，外加 `run_batch_edem.ps1`，按顺序把每个工况丢给 `edem.exe -c`。

## 上游依赖

| 依赖 | 用途 |
|---|---|
| `h5py` | 读写 `0.h5` / `.dem` |
| `numpy` | 复合 dataset 的整表往返（必须整表读出、改动、整体写回） |
| `openpyxl` | 仅批量模式 |
| Altair EDEM（2020–2022.2） | 真正求解用。本工具从不启动 GUI。 |

## 仓库结构

```
SKILL.md                       给 agent 读的技能说明
scripts/edem_set_param.py      单 deck 编辑器（体检 + 修改）
scripts/edem_batch_from_xlsx.py 参数矩阵 → N 套 deck + 求解队列
examples/cases.example.csv     输入表列布局
```

`SKILL.md` 写给任何会读 skill 清单的 agent——把宿主指向它，或者直接调脚本。仓库里没有任何东西绑定特定宿主或厂商。

## 边界

实测基线 **EDEM 2022.1**（HDF5 `file version = 2622234`，`.dfg` `file-version = 30.0.3`）。同代版本预期成立，跨大版本不保证。

- **只处理单球（single-sphere）** 的半径/质量/惯量联动。多球团簇还要等比缩放子球 `pos`；多面体还要缩放顶点网格。变通办法：求解时挂 `--particle-properties` 交给内核重算。
- **只匹配 `Linear Rotation` 运动学。** 振动筛（`Sinusoidal Translation/Rotation`）、皮带机、多级父子运动需要本版没有的分派表。
- **扩展接触模型**（JKR、Bonding V2、液桥）的参数存在独立插件组，不在基础 `Interactions` 表里，暂不支持。
- **格式是逆向的。** `file version` 和 `.dfg` 键是 Altair 内部格式，不是公开契约。在陌生版本上写入前先 `--list` 核对，并留着自动备份。
- 工具只读写你自己已有的仿真文件。跑求解仍然需要合法的 Altair EDEM 授权。

## 许可

MIT，见 [LICENSE](LICENSE)。
