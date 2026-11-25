#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Generic EDEM Batch Case Generator v0.1.0 (.csv / .xlsx -> N clean 0-second decks + solver queue)

Reads a declarative parameter matrix from CSV (stdlib, zero extra dependencies) or Excel
(.xlsx via openpyxl), clones clean 0-second decks from a calibrated template, applies all
per-row edits, updates sibling .dfg solver settings, and emits PowerShell (.ps1) and/or
Bash (.sh) batch scripts for headless `edem -c` execution.

Declarative column syntax (works for ANY EDEM deck, any number of species/factories/contacts):
  case_id                       Required case folder/file identifier (aliases: 工况编号, case, id)
  case_type / notes             Optional label for summary output
  rpm | rpm:<geom>              Linear Rotation speed in r/min
  speed_rad | speed_rad:<geom>  Linear Rotation speed in rad/s
  kin:<geom>:<attr>             Any kinematic attribute (e.g. kin:Screen:amplitude)
  d_mm:<target>                 Particle diameter in mm (blank or <=0 safely skipped)
  r_m:<target>                  Particle radius in m (blank or <=0 safely skipped)
  scale:<target>                Uniform size scale factor (single or multi-sphere)
  count:<factory>               Factory particle count (0 safely sets enabled=0)
  mass_kg:<factory>             Factory total mass in kg (0 safely sets enabled=0)
  rate:<factory>                Factory creation rate (/s or kg/s)
  fill_duration | fill_duration_s  Per-row fill duration override (s)
  density:<mat> | shear:<mat> | poisson:<mat>
  interaction:<A~B>             "e,sf,rf" triple
  e:<A~B> | sf:<A~B> | rf:<A~B> Single contact property (ideal for calibration sweeps)
  periodic_x | periodic_y | periodic_z  (0 or 1; untouched unless column/CLI flag provided)
  gravity                       "gx,gy,gz"
  timestep_s | timestep_us      Fixed timestep in s or microseconds
  cell_size_m                   Absolute grid cell size in m
  sim_time_s | save_interval_s | track_collisions | threads

Also keeps full backward compatibility with legacy two-species ball-mill headers
(steel_d_mm / 钢球直径, ceramic_d_mm / 瓷球直径, steel_count / 钢球数量, ceramic_count / 瓷球数量,
目标转速, 步长/timestep_us) via --steel-factory / --ceramic-factory / --steel-material / --ceramic-material.
"""
import argparse
import csv
import os
import re
import shutil
import sys

import h5py
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import edem_set_param as esp  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------- Column parsing

PREFIX_MAP = {
    'rpm': 'rpm',
    'speed_rad': 'speed_rad',
    'speed': 'speed_rad',
    'kin': 'kin',
    'd_mm': 'd_mm',
    'diameter_mm': 'd_mm',
    'r_m': 'r_m',
    'radius_m': 'r_m',
    'scale': 'scale',
    'count': 'count',
    'mass_kg': 'mass_kg',
    'rate': 'rate',
    'density': 'density',
    'shear': 'shear',
    'poisson': 'poisson',
    'interaction': 'interaction',
    'e': 'e',
    'restitution': 'e',
    'sf': 'sf',
    'static_friction': 'sf',
    'rf': 'rf',
    'rolling_friction': 'rf',
}

EXACT_SCALAR_COLS = {
    'case_id': 'case_id',
    'caseid': 'case_id',
    'case': 'case_id',
    'id': 'case_id',
    '工况编号': 'case_id',
    '工况号': 'case_id',
    '编号': 'case_id',
    'case_type': 'case_type',
    'casetype': 'case_type',
    'type': 'case_type',
    'notes': 'case_type',
    '工况类型': 'case_type',
    '组别类型': 'case_type',
    '类型': 'case_type',
    'rpm': 'rpm',
    'speed_rad': 'speed_rad',
    'fill_duration': 'fill_duration',
    'fill_duration_s': 'fill_duration',
    'periodic_x': 'periodic_x',
    'periodic_y': 'periodic_y',
    'periodic_z': 'periodic_z',
    'gravity': 'gravity',
    'timestep_s': 'timestep_s',
    'dt_s': 'timestep_s',
    'timestep_us': 'timestep_us',
    'dt_us': 'timestep_us',
    'cell_size_m': 'cell_size_m',
    'sim_time_s': 'sim_time_s',
    'sim_time': 'sim_time_s',
    'save_interval_s': 'save_interval_s',
    'save_interval': 'save_interval_s',
    'track_collisions': 'track_collisions',
    'threads': 'threads',
}

# Fuzzy legacy aliases (for spreadsheets with units/annotations in headers)
LEGACY_FUZZY_ALIASES = {
    'case_id':       ['case_id', '工况编号', '工况号', '编号'],
    'case_type':     ['case_type', '工况类型', '组别类型'],
    'rpm':           ['target speed', 'mill speed', '目标转速'],
    'legacy_ds_mm':  ['steel_d_mm', 'steel diameter', 'steel_d', '钢球直径', '钢球粒径', '钢介质直径'],
    'legacy_dc_mm':  ['ceramic_d_mm', 'ceramic diameter', 'ceramic_d', '瓷球直径', '瓷球粒径', '陶瓷球直径'],
    'legacy_ns':     ['steel_count', 'steel balls', 'steel_n', '钢球个数', '钢球数', '钢球数量'],
    'legacy_nc':     ['ceramic_count', 'ceramic balls', 'ceramic_n', '瓷球个数', '瓷球数', '瓷球数量'],
    'timestep_us':   ['timestep_us', 'dt_us', '时间步长', '时间步', '步长'],
}


def _norm(v):
    """Normalize header text for alias matching (lowercase, strip whitespace, fullwidth->ascii)."""
    s = '' if v is None else str(v)
    s = s.strip().lower()
    for a, b in (('（', '('), ('）', ')'), ('：', ':'), ('μ', 'u'), ('µ', 'u')):
        s = s.replace(a, b)
    return re.sub(r'\s+', '', s)


def _is_blank(v):
    if v is None:
        return True
    if isinstance(v, str) and v.strip() in ('', '-', 'NA', 'N/A', 'None', 'null'):
        return True
    return False


def _parse_float(v, row_num, code, col_label):
    try:
        return float(v)
    except (TypeError, ValueError):
        sys.exit(f"[ERROR] 第 {row_num} 行（工况 {code}）列 '{col_label}' 不是有效数字: {v!r}")


def parse_header_cell(raw_header, legacy_cfg):
    """Parse a single header cell into an action descriptor dict, or None if ignored."""
    if raw_header is None:
        return None
    raw_str = str(raw_header).strip()
    if not raw_str:
        return None

    # 1. Explicit prefixed syntax: preserve original case of target (after first ':')
    raw_ascii_colon = raw_str.replace('：', ':')
    if ':' in raw_ascii_colon:
        prefix_raw, target = raw_ascii_colon.split(':', 1)
        prefix = _norm(prefix_raw)
        target = target.strip()
        if prefix in PREFIX_MAP and target:
            return {'kind': PREFIX_MAP[prefix], 'target': target, 'raw': raw_str}

    # 2. Exact scalar column match
    normed = _norm(raw_str)
    if normed in EXACT_SCALAR_COLS:
        return {'kind': EXACT_SCALAR_COLS[normed], 'target': None, 'raw': raw_str}

    # 3. Legacy fuzzy aliases (e.g. "目标转速 N (r/min)", "钢球直径 d_s (mm)", "最小 15%T_R 步长 (μs)")
    for key, aliases in LEGACY_FUZZY_ALIASES.items():
        for a in sorted((_norm(x) for x in aliases), key=len, reverse=True):
            if len(a) < 3 and not (len(a) == 2 and any('\u4e00' <= ch <= '\u9fff' for ch in a)):
                continue
            if normed == a or a in normed:
                if key == 'legacy_ds_mm':
                    return {'kind': 'd_mm', 'target': legacy_cfg['steel_material'], 'raw': raw_str, 'legacy': 'ds'}
                if key == 'legacy_dc_mm':
                    return {'kind': 'd_mm', 'target': legacy_cfg['ceramic_material'], 'raw': raw_str, 'legacy': 'dc'}
                if key == 'legacy_ns':
                    return {'kind': 'count', 'target': legacy_cfg['steel_factory'], 'raw': raw_str, 'legacy': 'ns'}
                if key == 'legacy_nc':
                    return {'kind': 'count', 'target': legacy_cfg['ceramic_factory'], 'raw': raw_str, 'legacy': 'nc'}
                return {'kind': key, 'target': None, 'raw': raw_str}

    return None


def _read_table_rows(matrix_path, sheet_name=None, data_start_row=2, header_row=None):
    """Read header row and data rows from .csv or .xlsx. Returns (sheet_label, header_row, headers, data_rows)."""
    ext = os.path.splitext(matrix_path)[1].lower()
    h_row = header_row or max(1, data_start_row - 1)

    if ext in ('.csv', '.tsv'):
        delim = '\t' if ext == '.tsv' else ','
        rows = None
        for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
            try:
                with open(matrix_path, 'r', encoding=enc, newline='') as fp:
                    rows = list(csv.reader(fp, delimiter=delim))
                break
            except UnicodeDecodeError:
                continue
        if rows is None:
            sys.exit(f"[ERROR] 无法解码表格文件: {matrix_path}")
        if h_row > len(rows):
            sys.exit(f"[ERROR] 表头行 {h_row} 超出文件总行数 {len(rows)}。")
        headers = rows[h_row - 1]
        data_rows = [(idx, rows[idx - 1]) for idx in range(data_start_row, len(rows) + 1)]
        return os.path.basename(matrix_path), h_row, headers, data_rows

    if ext in ('.xlsx', '.xlsm'):
        try:
            import openpyxl
        except ImportError:
            sys.exit("[ERROR] 读取 .xlsx 需要 openpyxl，请先安装: pip install openpyxl（或改用 .csv）")
        wb = openpyxl.load_workbook(matrix_path, data_only=True)
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        headers = [ws.cell(h_row, c).value for c in range(1, ws.max_column + 1)]
        data_rows = [
            (r, [ws.cell(r, c).value for c in range(1, ws.max_column + 1)])
            for r in range(data_start_row, ws.max_row + 1)
        ]
        return ws.title, h_row, headers, data_rows

    sys.exit(f"[ERROR] 不支持的矩阵格式 '{ext}'，仅支持 .csv / .tsv / .xlsx / .xlsm。")


def load_cases(matrix_path, legacy_cfg, sheet_name=None, data_start_row=2, header_row=None):
    """Parse CSV/XLSX into a list of structured case operation dicts."""
    label, h_row, headers, data_rows = _read_table_rows(
        matrix_path, sheet_name=sheet_name, data_start_row=data_start_row, header_row=header_row
    )
    print(f"[INFO] 表格='{label}', 表头行={h_row}, 数据起始行={data_start_row}")

    parsed_cols = {}
    seen_slots = set()
    raw_headers = {}
    for idx, h in enumerate(headers):
        if h is not None and str(h).strip():
            raw_headers[idx + 1] = str(h).strip()
        spec = parse_header_cell(h, legacy_cfg)
        if spec is None:
            continue
        slot = (spec['kind'], spec['target'])
        if slot not in seen_slots:
            seen_slots.add(slot)
            parsed_cols[idx] = spec

    case_id_col = next((i for i, s in parsed_cols.items() if s['kind'] == 'case_id'), None)
    if case_id_col is None:
        print(f"[ERROR] 第 {h_row} 行未识别到 case_id（工况编号）列。", file=sys.stderr)
        print(f"        该行识别到的表头: {raw_headers}", file=sys.stderr)
        print("        可用 --header-row / --data-start-row 指定真正的表头与数据起始行。", file=sys.stderr)
        sys.exit(1)

    param_cols = {i: s for i, s in parsed_cols.items() if s['kind'] not in ('case_id', 'case_type')}
    if not param_cols:
        print(f"[ERROR] 第 {h_row} 行除 case_id 外未识别到任何有效参数列。", file=sys.stderr)
        print(f"        该行识别到的表头: {raw_headers}", file=sys.stderr)
        print("        支持的声明式列名示例: rpm, d_mm:<颗粒>, count:<工厂>, sf:<A~B>, timestep_s 等。", file=sys.stderr)
        sys.exit(1)

    shown = ", ".join(
        f"{s['kind']}" + (f":{s['target']}" if s['target'] else "") + f"<-'{s['raw']}'"
        for _, s in sorted(parsed_cols.items())
    )
    print(f"[INFO] 识别列 ({len(parsed_cols)}): {shown}")

    cases = []
    for row_num, vals in data_rows:
        code_raw = vals[case_id_col] if case_id_col < len(vals) else None
        if _is_blank(code_raw):
            continue
        code = str(code_raw).strip()
        if code.startswith('汇总') or code.startswith('#'):
            continue
        if re.search(r'[\\/:*?"<>|]', code) or code in ('.', '..'):
            sys.exit(f"[ERROR] 第 {row_num} 行 case_id 包含非法路径字符: {code!r}")

        case = {
            'code': code,
            'type': '',
            'ops': [],
            'dfg': {},
            'fill_duration': None,
            'counts': {},
            'masses': {},
            'dt_s': None,
            'cell_size_m': None,
            'sim_time_s': None,
            'save_interval_s': None,
            'track_collisions': None,
            'threads': None,
        }

        for idx, spec in sorted(parsed_cols.items()):
            v = vals[idx] if idx < len(vals) else None
            kind = spec['kind']
            target = spec['target']
            if kind == 'case_id':
                continue
            if kind == 'case_type':
                case['type'] = '' if _is_blank(v) else str(v).strip()
                continue
            if _is_blank(v):
                continue

            if kind in ('rpm', 'speed_rad', 'd_mm', 'r_m', 'scale', 'rate',
                        'density', 'shear', 'poisson', 'e', 'sf', 'rf'):
                fval = _parse_float(v, row_num, code, spec['raw'])
                if kind in ('d_mm', 'r_m', 'scale') and fval <= 0:
                    # Inactive species in single-species cases often have d=0; skip safely.
                    continue
                case['ops'].append((kind, target, fval))
            elif kind == 'count':
                nval = int(round(_parse_float(v, row_num, code, spec['raw'])))
                case['counts'][target] = nval
                case['ops'].append(('count', target, nval))
            elif kind == 'mass_kg':
                mval = _parse_float(v, row_num, code, spec['raw'])
                case['masses'][target] = mval
                case['ops'].append(('mass_kg', target, mval))
            elif kind == 'kin':
                case['ops'].append(('kin', target, str(v).strip()))
            elif kind == 'interaction':
                case['ops'].append(('interaction', target, str(v).strip()))
            elif kind == 'gravity':
                case['ops'].append(('gravity', None, str(v).strip()))
            elif kind in ('periodic_x', 'periodic_y', 'periodic_z'):
                ax = kind.split('_')[1]
                nv = 1 if int(round(_parse_float(v, row_num, code, spec['raw']))) != 0 else 0
                case['ops'].append(('periodic', ax, nv))
            elif kind == 'fill_duration':
                case['fill_duration'] = _parse_float(v, row_num, code, spec['raw'])
            elif kind == 'timestep_s':
                case['dt_s'] = _parse_float(v, row_num, code, spec['raw'])
            elif kind == 'timestep_us':
                case['dt_s'] = _parse_float(v, row_num, code, spec['raw']) * 1e-6
            elif kind == 'cell_size_m':
                case['cell_size_m'] = _parse_float(v, row_num, code, spec['raw'])
            elif kind == 'sim_time_s':
                case['sim_time_s'] = _parse_float(v, row_num, code, spec['raw'])
            elif kind == 'save_interval_s':
                case['save_interval_s'] = _parse_float(v, row_num, code, spec['raw'])
            elif kind == 'track_collisions':
                case['track_collisions'] = 1 if int(round(_parse_float(v, row_num, code, spec['raw']))) != 0 else 0
            elif kind == 'threads':
                case['threads'] = int(round(_parse_float(v, row_num, code, spec['raw'])))

        cases.append(case)
    return cases


# ---------------------------------------------------------------- Cloning & execution

def clone_clean_deck(template_dem, dst_dir, case_name):
    """Clone a clean 0-second deck to dst_dir/<case_name>/<case_name>.dem."""
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


def _format_case_preview(case):
    parts = []
    for kind, target, val in case['ops']:
        label = f"{kind}:{target}" if target else kind
        parts.append(f"{label}={val}")
    if case['dt_s'] is not None:
        parts.append(f"dt={case['dt_s']:.3e}s")
    if case['cell_size_m'] is not None:
        parts.append(f"cell={case['cell_size_m']}m")
    if case['sim_time_s'] is not None:
        parts.append(f"T={case['sim_time_s']}s")
    tag = f" ({case['type']})" if case['type'] else ""
    return f"  [DRY] {case['code']}{tag} | " + ", ".join(parts)


def _apply_case_to_h5(cd, case, args):
    fill_dur = case['fill_duration'] if case['fill_duration'] is not None else args.fill_duration

    # Apply explicit CLI periodic overrides first (never hardcoded!)
    if args.periodic:
        esp.set_periodic(cd, args.periodic)

    # Order matters: materials -> sizes -> counts/masses -> rates -> contacts -> kinematics -> domain/periodic
    order_rank = {
        'density': 10, 'shear': 11, 'poisson': 12,
        'd_mm': 20, 'r_m': 21, 'scale': 22,
        'count': 30, 'mass_kg': 31, 'rate': 32,
        'interaction': 40, 'e': 41, 'sf': 42, 'rf': 43,
        'rpm': 50, 'speed_rad': 51, 'kin': 52,
        'periodic': 60, 'gravity': 61,
    }
    sorted_ops = sorted(case['ops'], key=lambda x: order_rank.get(x[0], 99))

    for kind, target, val in sorted_ops:
        if kind == 'density':
            esp.set_materials(cd, [f"{target}={val}"], 'density')
        elif kind == 'shear':
            esp.set_materials(cd, [f"{target}={val}"], 'shearMod')
        elif kind == 'poisson':
            esp.set_materials(cd, [f"{target}={val}"], 'poisson')
        elif kind == 'd_mm':
            esp.set_radius(cd, val / 2000.0, target)
        elif kind == 'r_m':
            esp.set_radius(cd, val, target)
        elif kind == 'scale':
            esp.set_scale(cd, val, target)
        elif kind == 'count':
            esp.set_ball_count(cd, [f"{target}={val}"], fill_duration=fill_dur)
        elif kind == 'mass_kg':
            esp.set_factory_mass(cd, [f"{target}={val}"], fill_duration=fill_dur)
        elif kind == 'rate':
            esp.set_factory_rate(cd, [f"{target}={val}"])
        elif kind == 'interaction':
            esp.set_interactions(cd, [f"{target}={val}"])
        elif kind == 'e':
            esp.set_interaction_field(cd, [f"{target}={val}"], 'restitution')
        elif kind == 'sf':
            esp.set_interaction_field(cd, [f"{target}={val}"], 'staticFriction')
        elif kind == 'rf':
            esp.set_interaction_field(cd, [f"{target}={val}"], 'rollingFriction')
        elif kind == 'rpm':
            esp.set_speed_rpm(cd, val, clockwise=args.clockwise, geom_filter=target)
        elif kind == 'speed_rad':
            esp.set_speed_rad(cd, val, geom_filter=target)
        elif kind == 'kin':
            esp.set_kinematic_attr(cd, [f"{target}={val}"])
        elif kind == 'periodic':
            esp.set_periodic(cd, [f"{target}={val}"])
        elif kind == 'gravity':
            vec = [float(x) for x in val.split(',')]
            if len(vec) != 3:
                sys.exit(f"[ERROR] 工况 {case['code']} gravity 需要 x,y,z 三个数: {val!r}")
            esp.set_gravity(cd, vec)


def _write_queue_scripts(out_dir, generated, args):
    shells = ['ps1', 'sh'] if args.shell == 'both' else ([] if args.shell == 'none' else [args.shell])
    if not shells:
        return

    edem_bin = os.environ.get('EDEM_BIN', '').strip()

    if 'ps1' in shells:
        ps1_path = os.path.join(out_dir, 'run_batch_edem.ps1')
        if edem_bin:
            edem_line = f'$EDEM = "{edem_bin}"'
        else:
            edem_line = ('# 未检测到环境变量 EDEM_BIN，请改成你的 edem.exe 全路径\n'
                         '$EDEM = Join-Path $env:ProgramFiles "Altair/2022.1/EDEM/bin/edem.exe"')
            print("[WARN] 环境变量 EDEM_BIN 未设置，生成的 ps1 里已留占位路径，运行前请改。")
        lines = [
            '# EDEM 控制台排队批处理求解脚本（自动生成）',
            edem_line,
            '$ErrorActionPreference = "Stop"',
            '',
        ]
        for item in generated:
            code = item['code']
            dem_rel = f"{code}\\{code}.dem"
            flags = ['-c', '-i', f'"$PSScriptRoot\\{dem_rel}"', '-R']
            if not args.no_particle_properties:
                flags.append('--particle-properties')
            if args.gpu:
                flags.extend(['-E', '2'])
            if item['sim_time'] is not None:
                flags.extend(['-r', f"{item['sim_time']}"])
            if item['save_interval'] is not None:
                flags.extend(['-w', f"{item['save_interval']}"])
            if item['dt_s'] is not None:
                flags.extend(['-t', f"{item['dt_s']:.6e}"])
            if not item.get('custom_cell') and args.grid_factor is not None and args.grid_factor > 0:
                flags.extend(['-g', f"{args.grid_factor:g}"])
            if item['threads'] is not None:
                flags.extend(['-p', f"{item['threads']}"])
            if args.extra_solver_args:
                flags.append(args.extra_solver_args.strip())
            lines.append(f'Write-Host "=== Running {code} ==="')
            lines.append('& $EDEM ' + ' '.join(flags))
        with open(ps1_path, 'w', encoding='utf-8-sig') as fp:
            fp.write('\r\n'.join(lines) + '\r\n')
        print(f"[QUEUE] 已生成 PowerShell 排队脚本: {ps1_path}")

    if 'sh' in shells:
        sh_path = os.path.join(out_dir, 'run_batch_edem.sh')
        lines = [
            '#!/usr/bin/env bash',
            'set -euo pipefail',
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'EDEM_BIN="${EDEM_BIN:-edem}"',
            '',
        ]
        for item in generated:
            code = item['code']
            dem_rel = f"{code}/{code}.dem"
            flags = ['-c', '-i', f'"${{SCRIPT_DIR}}/{dem_rel}"', '-R']
            if not args.no_particle_properties:
                flags.append('--particle-properties')
            if args.gpu:
                flags.extend(['-E', '2'])
            if item['sim_time'] is not None:
                flags.extend(['-r', f"{item['sim_time']}"])
            if item['save_interval'] is not None:
                flags.extend(['-w', f"{item['save_interval']}"])
            if item['dt_s'] is not None:
                flags.extend(['-t', f"{item['dt_s']:.6e}"])
            if not item.get('custom_cell') and args.grid_factor is not None and args.grid_factor > 0:
                flags.extend(['-g', f"{args.grid_factor:g}"])
            if item['threads'] is not None:
                flags.extend(['-p', f"{item['threads']}"])
            if args.extra_solver_args:
                flags.append(args.extra_solver_args.strip())
            lines.append(f'echo "=== Running {code} ==="')
            lines.append('"$EDEM_BIN" ' + ' '.join(flags))
        with open(sh_path, 'w', encoding='utf-8', newline='\n') as fp:
            fp.write('\n'.join(lines) + '\n')
        print(f"[QUEUE] 已生成 Bash 排队脚本: {sh_path}")


# ---------------------------------------------------------------- CLI entrypoint

def main(argv=None):
    ap = argparse.ArgumentParser(
        description='从 CSV / Excel 参数矩阵批量克隆生成 EDEM 0 秒工况 deck（v0.1.0 通用版）'
    )
    ap.add_argument('--template', required=True, help='已校准好的母版 .dem 路径')
    ap.add_argument('--matrix', '--xlsx', dest='matrix', required=True,
                    help='参数矩阵文件路径（支持 .csv / .tsv / .xlsx）')
    ap.add_argument('--sheet', default=None, help='Excel 工作表名（仅 .xlsx 生效），默认活动表')
    ap.add_argument('--data-start-row', type=int, default=2, help='首个数据行号（1-based），默认 2')
    ap.add_argument('--header-row', type=int, default=None, help='表头行号，默认取 data-start-row 的上一行')
    ap.add_argument('--out-dir', required=True, help='输出工况根目录')

    # Solver defaults (None = preserve template .dfg unless specified in table or CLI)
    ap.add_argument('--sim-time', type=float, default=None, help='覆盖总仿真时长 (s)；不传则保留母版或表内值')
    ap.add_argument('--save-interval', type=float, default=None, help='覆盖保存帧间隔 (s)；不传则保留母版或表内值')
    ap.add_argument('--fill-duration', type=float, default=None, help='工厂下料完成时长 (s)，用于自动折算 creation rate')
    ap.add_argument('--threads', type=int, default=None, help='覆盖 CPU 求解线程数；不传则保留母版或表内值')
    ap.add_argument('--track-collisions', type=int, choices=[0, 1], default=None,
                    help='覆盖 Track Collisions 开关 (1/0)；不传则保留母版或表内值')
    ap.add_argument('--grid-factor', type=float, default=2.5,
                    help='自动搜索网格尺寸倍率 (R_min × factor，设为 0 则不自动覆盖网格)，默认 2.5')
    ap.add_argument('--periodic', nargs='+', metavar='AXIS=0|1',
                    help='对全部派生工况统一设置周期边界（如 --periodic z=0）；不传则保留母版设置')
    ap.add_argument('--clockwise', action='store_true', help='按 +Z 正视顺时针方向设 rpm 角速度符号')

    # Queue script options
    ap.add_argument('--shell', choices=['ps1', 'sh', 'both', 'none'], default='ps1',
                    help='生成的排队求解脚本格式，默认 ps1')
    ap.add_argument('--gpu', action='store_true', help='排队脚本启用 CUDA GPU 求解器 (-E 2)')
    ap.add_argument('--no-particle-properties', action='store_true',
                    help='排队脚本不附加 --particle-properties')
    ap.add_argument('--extra-solver-args', default='', help='附加到 edem -c 命令末尾的额外参数')

    # Legacy two-species ball-mill compatibility overrides
    ap.add_argument('--steel-factory', default='Steel_factory', help='（兼容旧表头）钢球工厂名')
    ap.add_argument('--ceramic-factory', default='Ceramic_factory', help='（兼容旧表头）瓷球工厂名')
    ap.add_argument('--steel-material', default='Steel', help='（兼容旧表头）钢球材料名或颗粒名')
    ap.add_argument('--ceramic-material', default='Ceramic', help='（兼容旧表头）瓷球材料名或颗粒名')

    ap.add_argument('--only', nargs='*', help='仅生成指定工况编号（如 C01 C02）')
    ap.add_argument('--dry-run', action='store_true', help='仅预览解析结果，不写磁盘')
    args = ap.parse_args(argv)

    legacy_cfg = {
        'steel_factory': args.steel_factory,
        'ceramic_factory': args.ceramic_factory,
        'steel_material': args.steel_material,
        'ceramic_material': args.ceramic_material,
    }
    cases = load_cases(
        args.matrix,
        legacy_cfg=legacy_cfg,
        sheet_name=args.sheet,
        data_start_row=args.data_start_row,
        header_row=args.header_row,
    )
    if args.only:
        allow = set(args.only)
        cases = [c for c in cases if c['code'] in allow]
    if not cases:
        sys.exit("[ERROR] 未匹配到任何工况行。")

    print(f"共读取到 {len(cases)} 组工况（母版: {args.template}）：")
    if args.dry_run:
        for c in cases:
            print(_format_case_preview(c))
        return

    os.makedirs(args.out_dir, exist_ok=True)
    generated = []

    for c in cases:
        print(f"\n--- 派生工况 {c['code']} ---")
        dst_dem, dst_h5, dst_dfg = clone_clean_deck(args.template, args.out_dir, c['code'])
        with h5py.File(dst_h5, 'a') as f:
            cd = f['CreatorData/0']
            _apply_case_to_h5(cd, c, args)
            r_min_m = esp.get_active_min_radius(cd)

        dfg_updates = {}
        dt_s = c['dt_s']
        if dt_s is not None:
            dfg_updates['PROCESSOR:GENERAL:AUTO TIMESTEP'] = 'BOOLEAN:FALSE'
            dfg_updates['PROCESSOR:GENERAL:FIXED TIMESTEP'] = f'DOUBLE:{dt_s:.6e}'

        cell_m = c['cell_size_m']
        if cell_m is None and args.grid_factor is not None and args.grid_factor > 0 and r_min_m is not None:
            # Update grid cell size when sizes or counts were modified, or dt was set
            has_size_or_count = any(k in ('d_mm', 'r_m', 'scale', 'count', 'mass_kg') for k, _, _ in c['ops'])
            if has_size_or_count:
                cell_m = round(args.grid_factor * r_min_m, 6)
        if cell_m is not None:
            dfg_updates['PROCESSOR:GENERAL:AUTO GRIDCELL'] = 'BOOLEAN:FALSE'
            dfg_updates['PROCESSOR:GENERAL:GRID CELL SIZE'] = f'DOUBLE:{cell_m:.6g}'

        track = c['track_collisions'] if c['track_collisions'] is not None else args.track_collisions
        if track is not None:
            dfg_updates['PROCESSOR:GENERAL:TRACK COLLISIONS'] = 'BOOLEAN:TRUE' if track else 'BOOLEAN:FALSE'

        threads = c['threads'] if c['threads'] is not None else args.threads
        if threads is not None:
            dfg_updates['PROCESSOR:GENERAL:NUM THREADS'] = f'INT:{threads}'

        save_iv = c['save_interval_s'] if c['save_interval_s'] is not None else args.save_interval
        if save_iv is not None:
            dfg_updates['PROCESSOR:WRITE:SELECTIVE SAVE'] = 'BOOLEAN:FALSE'
            dfg_updates['PROCESSOR:WRITE:INTERVAL'] = f'DOUBLE:{save_iv:.6g}'

        sim_t = c['sim_time_s'] if c['sim_time_s'] is not None else args.sim_time
        if sim_t is not None:
            dfg_updates['PROCESSOR:WRITE:TOTAL'] = f'DOUBLE:{sim_t:.6g}'

        if dfg_updates:
            esp.update_dfg(dst_dfg, dfg_updates)

        # Read back final .dfg values for queue script fidelity
        final_dfg = esp.read_dfg_summary(dst_dfg) or {}
        generated.append({
            'code': c['code'],
            'custom_cell': c['cell_size_m'] is not None,
            'dt_s': dt_s if dt_s is not None else esp.parse_dfg_typed_value(final_dfg.get('PROCESSOR:GENERAL:FIXED TIMESTEP')),
            'sim_time': sim_t if sim_t is not None else esp.parse_dfg_typed_value(final_dfg.get('PROCESSOR:WRITE:TOTAL')),
            'save_interval': save_iv if save_iv is not None else esp.parse_dfg_typed_value(final_dfg.get('PROCESSOR:WRITE:INTERVAL')),
            'threads': threads if threads is not None else esp.parse_dfg_typed_value(final_dfg.get('PROCESSOR:GENERAL:NUM THREADS')),
        })

    _write_queue_scripts(args.out_dir, generated, args)
    print(f"\n[BATCH DONE] 已生成 {len(generated)} 套干净 0 秒 deck。")


if __name__ == '__main__':
    main()
