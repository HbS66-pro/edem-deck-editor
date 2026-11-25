# edem-deck-editor

**在命令行改 Altair EDEM 仿真 deck，不点 GUI，也不需要前处理 API。**

[English →](README.md)

一个通用的 agent skill + 两个 Python CLI，用 `h5py` 直接读写 EDEM 的 deck 文件。它最初源自一次球磨机 DEM 参数研究，现已通用化为适用于**任意** EDEM 2020–2022.2 仿真场景的工具：休止角/料仓接触参数标定扫描、单/多物种颗粒研究、滚筒/搅拌器/振动筛改参，以及从 CSV/Excel 矩阵一键派生批量工况与求解队列。

## 能做什么

| 能力 | 值落在哪 | 防呆与联动 |
|---|---|---|
| 几何运动学（旋转与通用） | `GeometryGroups/*/Kinematics/*` | `--rpm` / `--speed` + `--geometry` 定向选择 + `--clockwise`；`--kinematic-attr` 保留原 HDF5 dtype 修改平动/振动/起止时间等任意属性 |
| 颗粒半径 / 缩放 | `ParticleTypes/*/spheres` | 单球按材料密度重算 `raw volume/mass/inertia` 并置 `auto properties=1`；多球等比缩放子球半径与 `pos`，提示求解挂 `--particle-properties` |
| 工厂（按数量与按质量） | `Factories/*`（`num to create` / `mass to create`） | 传 `0` 自动设 `enabled=0` 安全停用；`--fill-duration` 反推速率；`--list` 报重绑告警 |
| 材料与接触 | `Materials`、`Interactions` 复合表 | **改材料密度自动联动重算单球质量与惯量**；支持整组 `e,sf,rf` 或单项 `--interaction-e/-sf/-rf`（适合标定扫描） |
| 周期边界与重力 | `Domain@domain {x,y,z}Periodic`、`@gravity` | 仅在显式传参或表内存在对应列时修改，不静默改动母版 |
| 接触模型启停 | `PhysicsModels/*/order` | 同步 `enable rolling friction` 属性 |
| 求解器设置 | `.dfg` 的 `PROCESSOR:GENERAL:*` / `PROCESSOR:WRITE:*` | 时间步、网格、Track Collisions、保存间隔、总时长、线程数；未指定项保留母版原值 |
| 自动导出矩阵模板 | 任意 `.dem` → 起始 `.csv` | `--export-template-csv` 扫描 deck 自动生成带准确列名的通用 CSV 模板 |
| 通用批量派生 | `.csv` / `.xlsx` → N 套干净 0 秒 deck | 任意物种/工厂/接触数量；支持生成 Windows `.ps1` 与 Linux/HPC `.sh` 排队脚本 |

## 为什么不用 `edempy` 或 MCP Server？

- **为什么不用 `edempy`？**
  - **EDEM ≤ 2022.2**：自带的 `edempy`（2022.1 上是 0.2.0）仅支持后处理只读，没有前处理写入 API，且在 `numpy >= 2.0` 下导入即崩（`numpy.string_` 被移除）。
  - **EDEM ≥ 2022.3**：Altair 补了官方 Deck Editing API（`Deck(..., mode='w')`）。如果你在新版本上，优先用官方契约。
  - **两者之间**：本工具直接写 EDEM 自己写的文件——`_data/0.h5`（`CreatorData/0`）与同名 `.dfg`（`KEYPATHVALUE` 纯文本）。
- **为什么用 Skill + 确定性 CLI 而不是 MCP Server？**
  - MCP Server 每轮对话都会向模型注入完整工具 Schema，不跑 EDEM 时也在付待机 token；如果让大模型逐个调工具改 20–100 组工况，多轮上下文会耗掉几十万 token 且容易参数漂移。
  - 薄 `SKILL.md` + 声明式 CSV/Excel 表 + 本地确定性 CLI，1 秒内稳定派生整批工况，token 消耗省 95% 以上且 100% 可复现。

## 安装

Python 3.8+。

```bash
pip install h5py numpy
# 可选：只有读取 .xlsx 矩阵时需要 openpyxl（读 .csv 零额外依赖）
pip install openpyxl
```

想让生成的批处理队列脚本自动定位求解器，设一次 `EDEM_BIN` 环境变量：

```powershell
# Windows PowerShell
$env:EDEM_BIN = Join-Path $env:ProgramFiles "Altair\2022.1\EDEM\bin\edem.exe"
```

```bash
# Linux / HPC 集群
export EDEM_BIN="/opt/altair/2022.1/edem/bin/edem"
```

## 用法

### 1. 体检任意 deck（只读）

```bash
python scripts/edem_set_param.py ./cases/template.dem --list
```

### 2. 修改单个工况

```bash
python scripts/edem_set_param.py ./cases/case.dem \
    --geometry Drum --rpm 40 --clockwise \
    --diameter-mm 18 --radius-material Ore \
    --factory-mass "Feed_Factory=5.0" --fill-duration 1.0 \
    --interaction-sf "Ore~Wall=0.45" --interaction-rf "Ore~Wall=0.05" \
    --timestep 5e-6
```

### 3. 从母版自动导出参数模板并批量派生

```bash
# 根据你的 deck 自动生成带准确工厂名/颗粒名/接触对名的 CSV 模板
python scripts/edem_set_param.py ./cases/template.dem --export-template-csv ./cases/matrix.csv

# 先 dry-run 预览解析结果（支持 .csv 和 .xlsx）
python scripts/edem_batch.py \
    --template ./cases/template.dem \
    --matrix ./cases/matrix.csv \
    --out-dir ./cases/batch \
    --dry-run

# 实际派生 0 秒工况并同时生成 Windows (.ps1) 和 Linux (.sh) 求解队列
python scripts/edem_batch.py \
    --template ./cases/template.dem \
    --matrix ./cases/matrix.csv \
    --out-dir ./cases/batch \
    --shell both
```

### 声明式矩阵列名协议

除 `case_id` 外，其余列均按需选用——只写你要扫描的参数即可：

- `case_id`（必填）、`case_type`（可选备注）
- `rpm` / `rpm:<几何名>`、`speed_rad` / `speed_rad:<几何名>`、`kin:<几何名>:<属性名>`
- `d_mm:<颗粒或材料>`、`r_m:<目标>`、`scale:<目标>`（空白或 `<= 0` 安全跳过，不会在单物种验证组报错）
- `count:<工厂名>`、`mass_kg:<工厂名>`、`rate:<工厂名>`、`fill_duration_s`
- `density:<材料>`、`shear:<材料>`、`poisson:<材料>`
- `interaction:<A~B>`（`e,sf,rf`）或单项 `e:<A~B>`、`sf:<A~B>`、`rf:<A~B>`
- `periodic_x`、`periodic_y`、`periodic_z`、`gravity`
- `timestep_s`、`timestep_us`、`cell_size_m`、`sim_time_s`、`save_interval_s`、`track_collisions`、`threads`

开箱即用的示例矩阵见 `examples/`：
- [`examples/contact_calibration.csv`](examples/contact_calibration.csv) — 摩擦系数/恢复系数标定扫描
- [`examples/single_species_sweep.csv`](examples/single_species_sweep.csv) — 单物种转速/粒径/给料质量扫描
- [`examples/cases.example.csv`](examples/cases.example.csv) — 双介质磨机矩阵（含单介质 `count=0` 验证行）

旧版双介质球磨机中文/英文表头及旧入口 `scripts/edem_batch_from_xlsx.py` 保持 100% 向后兼容。

## 输出

- 单文件模式原地修改，改前自动备份到 `<deck 目录>/_edem_backup/<deck>_<时间戳>/`（`.dem`、`0.h5`、`.dfg`）；`--no-backup` 可关。
- 批量模式输出 `--out-dir/<工况>/<工况>.dem` + `<工况>_data/0.h5` + 同名 `.dfg`/侧车文件（`num timesteps` 重置为 `1`），以及 `run_batch_edem.ps1` 和/或 `run_batch_edem.sh`。

## 上游依赖

| 依赖 | 用途 |
|---|---|
| `h5py` | 读写 `0.h5` / `.dem` |
| `numpy` | 复合 dataset 整表往返 |
| `openpyxl` | 可选，仅读取 `.xlsx` 时需要 |
| Altair EDEM（2020–2022.2） | 运行求解。本工具从不启动 GUI。 |

## 仓库结构

```
SKILL.md                         给 agent 读的通用技能说明（零宿主绑定）
scripts/edem_set_param.py        单 deck 体检、修改与 CSV 模板导出
scripts/edem_batch.py            通用 CSV/XLSX 矩阵 → N 套 deck + .ps1/.sh 队列
scripts/edem_batch_from_xlsx.py  向后兼容入口（委托给 edem_batch.py）
examples/                        示例矩阵（接触标定 / 单物种 / 双介质）
tests/test_edem_deck_editor.py   纯合成 HDF5 + .dfg 回归测试套件（无需安装 EDEM）
```

运行本地回归测试：

```bash
python -m unittest discover -s tests -v
```

## 边界

实测基线 **EDEM 2022.1**（HDF5 `file version = 2622234`，`.dfg` `file-version = 30.0.3`）。`--list` 会显示 `file_version` 并对未实测版本发出提醒。

- **多球/多面体质量惯量**：多球缩放会同步等比缩放子球半径与局域 `pos`，但重叠球团的真实质量与惯量张量需求解时挂 `--particle-properties` 由内核复算（自动生成的排队脚本已默认启用）。暂不支持多面体顶点网格缩放。
- **扩展接触模型插件**：JKR、Bonding V2、液桥等参数存放在独立插件组，不在基础 `Interactions` 表中，暂不支持。
- **格式是逆向的**：`file version` 和 `.dfg` 键是 Altair 内部格式，不是公开契约。在陌生版本写入前先 `--list` 核对并留着自动备份。
- 工具只读写你自己已有的本地仿真文件。跑求解仍需合法的 Altair EDEM 授权。

## 许可

MIT，见 [LICENSE](LICENSE)。
