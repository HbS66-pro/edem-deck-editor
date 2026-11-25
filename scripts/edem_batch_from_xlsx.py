#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从 Excel 参数矩阵一键派生多组干净 0 秒 EDEM 工况 deck，并生成控制台排队求解脚本。

特点：
1. 轻量克隆：只复制母版 .dem、_data/0.h5 与 .dfg/.efd/.ess/.ptf 侧车文件（自动剔除母版历史 1..N.h5 时间步，重置 num timesteps=1）。
2. 全参数到位：逐工况写入转速（支持顺时针修正）、钢/瓷球数量（0 颗自动停用对应工厂）、球径（联动重算体积/质量/转动惯量）、关闭 Z 向周期边界。
3. 同步写入 .dfg：逐工况写入时间步长、搜索网格尺寸、Track Collisions=TRUE、总时长与保存间隔。
4. 按表头名识别列（中英文别名均可），不依赖固定列序；缺列时直接报错并打印识别结果。
5. 自动生成排队运行脚本 run_batch_edem.ps1（调用 edem.exe -c 后台顺序求解；求解器路径取环境变量 EDEM_BIN）。
"""
import argparse
import os
import re
import shutil
import sys

import h5py
import numpy as np
import openpyxl
from openpyxl.utils import get_column_letter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import edem_set_param as esp


# ---------------------------------------------------------------- 列名识别

COLUMN_ALIASES = {
    'code':  ['case_id', 'caseid', 'case', '工况编号', '工况号', '编号'],
    'type':  ['case_type', 'casetype', 'type', '工况类型', '类型'],
    'rpm':   ['rpm', 'target speed', 'mill speed', '目标转速'],
    'ds_mm': ['steel_d_mm', 'steel diameter', 'steel_d', '钢球直径', '钢球粒径', '钢介质直径'],
    'dc_mm': ['ceramic_d_mm', 'ceramic diameter', 'ceramic_d', '瓷球直径', '瓷球粒径', '陶瓷球直径'],
    'ns':    ['steel_count', 'steel balls', 'steel_n', '钢球个数', '钢球数', '钢球数量'],
    'nc':    ['ceramic_count', 'ceramic balls', 'ceramic_n', '瓷球个数', '瓷球数', '瓷球数量'],
    'dt_us': ['timestep_us', 'timestep', 'dt_us', 'dt', '时间步', '时间步长', '步长'],
}
REQUIRED = ['code', 'rpm', 'ds_mm', 'dc_mm', 'ns', 'nc', 'dt_us']


def _norm(v):
    """表头归一化：小写、去空白、全角括号转半角、μ 转 u。"""
    s = '' if v is None else str(v)
    s = s.strip().lower()
    for a, b in (('（', '('), ('）', ')'), ('μ', 'u'), ('µ', 'u')):
        s = s.replace(a, b)
    return re.sub(r'\s+', '', s)


def resolve_columns(ws, header_row):
    """按表头名识别列，返回 ({key: col_index}, {col: 原始表头文本})。"""
    norm_alias = {k: [_norm(a) for a in v] for k, v in COLUMN_ALIASES.items()}
    headers, raw = {}, {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(header_row, c).value
        t = _norm(v)
        if t and t not in headers.values():
            headers[c] = t
            raw[c] = str(v).strip()

    mapping = {}
    for key, aliases in norm_alias.items():
        exact = next((c for c, t in headers.items() if t in aliases), None)
        if exact is not None:
            mapping[key] = exact
            continue
        for a in sorted(aliases, key=len, reverse=True):
            # 短别名只放行中文（如「步长」）；两字母英文缩写（如 dt）误配风险太高
            if len(a) < 3 and not (len(a) == 2 and any('\u4e00' <= ch <= '\u9fff' for ch in a)):
                continue
            hit = next((c for c, t in headers.items() if a in t), None)
            if hit is not None:
                mapping[key] = hit
                break
    return mapping, raw


# ---------------------------------------------------------------- 克隆与读取

def clone_clean_deck(template_dem, dst_dir, case_name):
    """从母版克隆一个干净的 0 秒 deck 到 dst_dir/<case_name>/<case_name>.dem。"""
    src_dem, _, src_h5, _ = esp.find_deck_files(template_dem)
    src_base = src_dem[:-4]

    case_dir = os.path.join(dst_dir, case_name)
    case_data = os.path.join(case_dir, f"{case_name}_data")
    os.makedirs(case_data, exist_ok=True)

    dst_dem = os.path.join(case_dir, f"{case_name}.dem")
    dst_h5 = os.path.join(case_data, "0.h5")
    shutil.copy2(src_dem, dst_dem)
    shutil.copy2(src_h5, dst_h5)

    with h5py.File(dst_dem, 'a') as fd:
        fd.attrs['num timesteps'] = np.int32(1)

    for ext in ('.dfg', '.efd', '.ess', '.ptf'):
        sp = src_base + ext
        if os.path.exists(sp):
            shutil.copy2(sp, os.path.join(case_dir, case_name + ext))

    return dst_dem, dst_h5, os.path.join(case_dir, case_name + '.dfg')


def load_cases_from_xlsx(xlsx_path, sheet_name=None, data_start_row=2, header_row=None):
    """读取工况矩阵；列位置由表头名决定，与列序无关。"""
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
    header_row = header_row or max(1, data_start_row - 1)

    col, raw = resolve_columns(ws, header_row)
    print(f"[INFO] 工作表='{ws.title}', 表头行={header_row}, 数据起始行={data_start_row}")

    missing = [k for k in REQUIRED if k not in col]
    if missing:
        print(f"[ERROR] 第 {header_row} 行缺少必需列: {missing}", file=sys.stderr)
        print(f"        该行识别到的表头: {raw}", file=sys.stderr)
        print(f"        这些键支持的别名: { {k: COLUMN_ALIASES[k] for k in missing} }", file=sys.stderr)
        print("        可用 --header-row 指定真正的表头行。", file=sys.stderr)
        sys.exit(1)

    shown = ", ".join(f"{k}={get_column_letter(v)}" for k, v in sorted(col.items()))
    print(f"[INFO] 列映射: {shown}")

    cases = []
    for r in range(data_start_row, ws.max_row + 1):
        code = ws.cell(r, col['code']).value
        if not code or str(code).strip().startswith('汇总'):
            continue
        code = str(code).strip()

        def num(key, default=None):
            v = ws.cell(r, col[key]).value
            if v in (None, ''):
                if default is not None:
                    return default
                sys.exit(f"[ERROR] 第 {r} 行（工况 {code}）的 {key} 列为空。")
            try:
                return float(v)
            except (TypeError, ValueError):
                sys.exit(f"[ERROR] 第 {r} 行（工况 {code}）的 {key} 列不是数字: {v!r}")

        ctype = ws.cell(r, col['type']).value if 'type' in col else ''
        cases.append({
            'code': code,
            'type': str(ctype or ''),
            'rpm': num('rpm'),
            'ds_mm': num('ds_mm', 25.0),
            'dc_mm': num('dc_mm', 25.0),
            'ns': int(num('ns')),
            'nc': int(num('nc')),
            'dt_s': num('dt_us') * 1e-6,
        })
    return cases


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description='从 Excel 参数矩阵批量克隆生成 EDEM 工况 deck')
    ap.add_argument('--template', required=True, help='已校准好的母版 .dem 路径')
    ap.add_argument('--xlsx', required=True, help='参数矩阵 .xlsx 路径（列按表头名识别，别名见 examples/cases.example.csv）')
    ap.add_argument('--sheet', default=None, help='工作表名，默认取当前活动表')
    ap.add_argument('--data-start-row', type=int, default=2, help='首个数据行号（1-based），默认 2')
    ap.add_argument('--header-row', type=int, default=None, help='表头所在行，默认取 data-start-row 的上一行')
    ap.add_argument('--out-dir', required=True, help='输出工况根目录')
    ap.add_argument('--steel-factory', default='Steel_factory', help='母版中钢球工厂名')
    ap.add_argument('--ceramic-factory', default='Ceramic_factory', help='母版中瓷球工厂名')
    ap.add_argument('--steel-material', default='Steel', help='母版中钢球材料名或颗粒名')
    ap.add_argument('--ceramic-material', default='Ceramic', help='母版中瓷球材料名或颗粒名')
    ap.add_argument('--sim-time', type=float, default=20.0, help='总仿真时长 (s)，默认 20.0')
    ap.add_argument('--save-interval', type=float, default=0.02, help='保存帧间隔 (s)，默认 0.02')
    ap.add_argument('--fill-duration', type=float, default=0.5, help='颗粒工厂落料完成时长 (s)，默认 0.5')
    ap.add_argument('--threads', type=int, default=8, help='CPU 核数，默认 8')
    ap.add_argument('--clockwise', action='store_true', help='按 +Z 正视顺时针方向设角速度符号')
    ap.add_argument('--only', nargs='*', help='仅生成指定工况编号（如 C01 C02），不传则生成全部')
    ap.add_argument('--dry-run', action='store_true', help='仅预览将生成的参数表，不写磁盘')
    args = ap.parse_args()

    cases = load_cases_from_xlsx(args.xlsx, args.sheet, args.data_start_row, args.header_row)
    if args.only:
        allow = set(args.only)
        cases = [c for c in cases if c['code'] in allow]
    if not cases:
        sys.exit("[ERROR] 未匹配到任何工况行。")

    print(f"共读取到 {len(cases)} 组工况（母版: {args.template}）：")
    for c in cases:
        r_min_m = min(c['ds_mm'] if c['ns'] > 0 else 999.0,
                      c['dc_mm'] if c['nc'] > 0 else 999.0) / 2000.0
        cell_m = round(2.5 * r_min_m, 5)
        if args.dry_run:
            print(f"  [DRY] {c['code']:5s} | {c['rpm']:4.0f} rpm | Ns={c['ns']:4d} (d={c['ds_mm']:.0f}mm), "
                  f"Nc={c['nc']:4d} (d={c['dc_mm']:.0f}mm) | dt={c['dt_s']:.3e} s | cell={cell_m} m")
            continue

        dst_dem, dst_h5, dst_dfg = clone_clean_deck(args.template, args.out_dir, c['code'])
        with h5py.File(dst_h5, 'a') as f:
            cd = f['CreatorData/0']
            esp.set_periodic(cd, ['z=0'])
            esp.set_speed_rpm(cd, c['rpm'], clockwise=args.clockwise)
            esp.set_radius(cd, c['ds_mm'] / 2000.0, args.steel_material)
            esp.set_radius(cd, c['dc_mm'] / 2000.0, args.ceramic_material)
            esp.set_ball_count(
                cd,
                [f"{args.steel_factory}={c['ns']}", f"{args.ceramic_factory}={c['nc']}"],
                fill_duration=args.fill_duration
            )
        esp.update_dfg(dst_dfg, {
            'PROCESSOR:GENERAL:AUTO TIMESTEP': 'BOOLEAN:FALSE',
            'PROCESSOR:GENERAL:FIXED TIMESTEP': f"DOUBLE:{c['dt_s']:.6e}",
            'PROCESSOR:GENERAL:AUTO GRIDCELL': 'BOOLEAN:FALSE',
            'PROCESSOR:GENERAL:GRID CELL SIZE': f"DOUBLE:{cell_m:.6g}",
            'PROCESSOR:GENERAL:TRACK COLLISIONS': 'BOOLEAN:TRUE',
            'PROCESSOR:GENERAL:NUM THREADS': f"INT:{args.threads}",
            'PROCESSOR:WRITE:SELECTIVE SAVE': 'BOOLEAN:FALSE',
            'PROCESSOR:WRITE:INTERVAL': f"DOUBLE:{args.save_interval:.6g}",
            'PROCESSOR:WRITE:TOTAL': f"DOUBLE:{args.sim_time:.6g}",
        })

    if not args.dry_run:
        ps1_path = os.path.join(args.out_dir, 'run_batch_edem.ps1')
        edem_bin = os.environ.get('EDEM_BIN', '').strip()
        if edem_bin:
            edem_line = f'$EDEM = "{edem_bin}"'
        else:
            edem_line = ('# 未检测到环境变量 EDEM_BIN，请改成你的 edem.exe 全路径\n'
                         '$EDEM = Join-Path $env:ProgramFiles "Altair/2022.1/EDEM/bin/edem.exe"')
            print("[WARN] 环境变量 EDEM_BIN 未设置，生成的脚本里已留占位路径，运行前请改。")
        lines = [
            '# EDEM 控制台排队批处理求解脚本（自动生成）',
            edem_line,
            '$ErrorActionPreference = "Stop"',
            ''
        ]
        for c in cases:
            dem_rel = os.path.join(c['code'], f"{c['code']}.dem")
            lines.append(f'Write-Host "=== Running {c["code"]} (dt={c["dt_s"]:.2e}s, T={args.sim_time}s) ==="')
            lines.append(
                f'& $EDEM -c -i "$PSScriptRoot\\{dem_rel}" -R --particle-properties '
                f'-r {args.sim_time} -w {args.save_interval} -t {c["dt_s"]:.6e} -g 2.5 -p {args.threads}'
            )
        with open(ps1_path, 'w', encoding='utf-8-sig') as fp:
            fp.write('\r\n'.join(lines) + '\r\n')
        print(f"\n[BATCH DONE] 已生成 {len(cases)} 套干净 deck 及控制台排队脚本: {ps1_path}")


if __name__ == '__main__':
    main()
