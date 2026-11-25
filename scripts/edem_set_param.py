#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
EDEM Deck Parameter Editor v0.1.0 (Generic Pre-Processing + Simulator .dfg Automation)

Directly reads and writes Altair EDEM deck files (.dem + _data/0.h5 + .dfg) via h5py,
bypassing GUI clicking and older edempy versions (<= 2022.2) that lack pre-processing
write APIs or crash under numpy >= 2.0.

Key capabilities:
1. Full deck inspection (--list / --list-models), including HDF5 file version check,
   all kinematics (not just Linear Rotation), particle types, factories, materials,
   interactions, domain/periodic boundaries, physics models, and .dfg solver settings.
2. Geometry-targeted kinematics: --geometry selects which rotating body to edit when
   multiple rotating geometries exist; --kinematic-attr edits any scalar/bool/vector
   attribute on any kinematic type (Linear Rotation/Translation, Sinusoidal, etc.).
3. Physics-consistent particle scaling: --radius / --diameter-mm / --scale updates
   single-sphere raw volume, raw mass, and raw inertia (and syncs raw mass/inertia
   automatically when --material-density changes); for multi-sphere clumps, scales
   child radii and local pos proportionally and advises --particle-properties at solve.
4. Safe factory control: --ball-count / --factory-mass with 0 safely disables the
   factory (enabled=0); supports --fill-duration rate derivation and --factory-type.
5. Materials & contacts: multi-item batch edits (--material-*, --interaction, plus
   single-field --interaction-e / --interaction-sf / --interaction-rf) with one backup.
6. Template export: --export-template-csv inspects any deck and emits a ready-to-fill
   generic parameter matrix CSV for edem_batch.py.
"""
import argparse
import csv
import datetime
import math
import os
import shutil
import sys

import h5py
import numpy as np

# Relax console encoding error handling on Windows GBK terminals so Unicode markers
# never crash stdout/stderr while keeping Chinese text readable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

TESTED_FILE_VERSIONS = {2622234}  # Altair EDEM 2022.1


# ---------------------------------------------------------------- Paths & backup

def find_deck_files(deck_path):
    """Resolve (.dem, _data dir, _data/0.h5, sibling .dfg) from a .dem path."""
    deck_path = os.path.abspath(deck_path)
    if not deck_path.lower().endswith('.dem'):
        deck_path += '.dem'
    if not os.path.exists(deck_path):
        sys.exit(f"[ERROR] deck 文件不存在: {deck_path}")
    base = deck_path[:-4]
    data_dir = base + '_data'
    h5_path = os.path.join(data_dir, '0.h5')
    dfg_path = base + '.dfg'
    if not os.path.exists(h5_path):
        sys.exit(f"[ERROR] 找不到数据文件: {h5_path}\n"
                 f"        .dem 的 _data 文件夹必须和它同名同目录。")
    return deck_path, data_dir, h5_path, dfg_path


def backup(deck_path, h5_path, dfg_path=None):
    """Lightweight pre-edit backup of .dem, 0.h5, and .dfg (if present)."""
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    base = os.path.basename(deck_path)[:-4]
    backup_dir = os.path.join(os.path.dirname(deck_path),
                              '_edem_backup', f'{base}_{stamp}')
    os.makedirs(backup_dir, exist_ok=True)
    shutil.copy2(deck_path, backup_dir)
    shutil.copy2(h5_path, backup_dir)
    if dfg_path and os.path.exists(dfg_path):
        shutil.copy2(dfg_path, backup_dir)
    print(f"[BACKUP] 已备份 (.dem / 0.h5 / .dfg) 到: {backup_dir}")
    return backup_dir


def _dec(v):
    if isinstance(v, (bytes, np.bytes_)):
        return v.decode('utf-8', errors='replace')
    return str(v)


def _match_name(query, *candidates):
    q = query.strip().lower()
    return any(c is not None and str(c).strip().lower() == q for c in candidates)


# ---------------------------------------------------------------- Discovery helpers

def list_kinematics(creator):
    """Return all kinematics as [(gk, geom_name, kk, kin_name, kin_type, kin_group), ...]."""
    gg = creator.get('GeometryGroups')
    if gg is None:
        return []
    result = []
    for gk in gg.keys():
        gnode = gg[gk]
        gname = _dec(gnode.attrs.get('name', b''))
        kin = gnode.get('Kinematics')
        if kin is not None and len(kin) > 0:
            for kk in kin.keys():
                kn = kin[kk]
                kname = _dec(kn.attrs.get('name', b''))
                ktype = _dec(kn.attrs.get('type', b''))
                result.append((gk, gname, kk, kname, ktype, kn))
    return result


def find_rotating_geometry(creator, geom_filter=None):
    """Locate a Linear Rotation kinematic, optionally filtered by geometry or kinematic name."""
    rotations = [item for item in list_kinematics(creator) if item[4] == 'Linear Rotation']
    if not rotations:
        return None, None
    if geom_filter:
        hits = [
            item for item in rotations
            if _match_name(geom_filter, item[0], item[1], item[3], f"{item[1]}/{item[3]}")
        ]
        if not hits:
            avail = [f"{it[1]} ({it[3]})" for it in rotations]
            sys.exit(f"[ERROR] 找不到匹配 '{geom_filter}' 的 Linear Rotation 几何。可用: {avail}")
        if len(hits) > 1:
            avail = [f"{it[1]}/{it[3]} (GG/{it[0]}/Kin/{it[2]})" for it in hits]
            sys.exit(f"[ERROR] '{geom_filter}' 匹配到多个旋转运动学: {avail}，请写 '几何名/运动名'。")
        return hits[0][0], hits[0][2]
    if len(rotations) > 1:
        avail = [f"{it[1]} ({it[3]})" for it in rotations]
        sys.exit(f"[ERROR] 检测到多个 Linear Rotation 几何: {avail}，请用 --geometry 指定目标。")
    return rotations[0][0], rotations[0][2]


def find_mill_geometry(creator):
    """Backward-compatible helper returning the first Linear Rotation (gk, kk)."""
    for gk, _, kk, _, ktype, _ in list_kinematics(creator):
        if ktype == 'Linear Rotation':
            return gk, kk
    return None, None


def list_factories(creator):
    """Return all particle factories [(gk, fk, factory_group), ...]."""
    gg = creator.get('GeometryGroups')
    if gg is None:
        return []
    result = []
    for gk in gg.keys():
        facs = gg[gk].get('Factories')
        if facs is not None:
            for fk in facs.keys():
                result.append((gk, fk, facs[fk]))
    return result


def get_material_densities(creator):
    """Return {material_name_str: float_density}."""
    mats = creator['Materials'][()]
    return {_dec(m['name']): float(m['density']) for m in mats}


def get_active_min_radius(creator):
    """Compute minimum physicalRadius (m) across active particles in the deck.

    If factories exist, prefers particle types bound to enabled factories; falls back
    to all particle types if no factory bindings are active.
    """
    enabled_targets = set()
    all_factories = list_factories(creator)
    for _, _, fg in all_factories:
        if int(fg.attrs.get('enabled', 1)) == 1 and 'FactType/type names' in fg:
            enabled_targets.add(_dec(fg['FactType/type names'][()]))

    pt = creator.get('ParticleTypes')
    if pt is None:
        return None

    active_radii = []
    all_radii = []
    for pk in pt.keys():
        p = pt[pk]
        pname = _dec(p.attrs.get('name', b''))
        spheres = p['spheres'][()]
        if len(spheres) == 0:
            continue
        r_min_p = float(np.min(spheres['physicalRadius']))
        if r_min_p <= 0:
            continue
        all_radii.append(r_min_p)
        if not enabled_targets or pname in enabled_targets:
            active_radii.append(r_min_p)

    pool = active_radii or all_radii
    return min(pool) if pool else None


# ---------------------------------------------------------------- .dfg helpers

DFG_KEYS_INTEREST = [
    'PROCESSOR:GENERAL:AUTO TIMESTEP',
    'PROCESSOR:GENERAL:FIXED TIMESTEP',
    'PROCESSOR:GENERAL:AUTO GRIDCELL',
    'PROCESSOR:GENERAL:GRID CELL SIZE',
    'PROCESSOR:GENERAL:TRACK COLLISIONS',
    'PROCESSOR:GENERAL:NUM THREADS',
    'PROCESSOR:WRITE:SELECTIVE SAVE',
    'PROCESSOR:WRITE:INTERVAL',
    'PROCESSOR:WRITE:TOTAL',
]


def read_dfg_summary(dfg_path):
    """Read key Simulator parameters from sibling .dfg file."""
    if not os.path.exists(dfg_path):
        return None
    found = {}
    with open(dfg_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            if s.startswith('!file-version='):
                found['!file-version'] = s.split('=', 1)[1]
            for k in DFG_KEYS_INTEREST:
                if s.startswith(k + '='):
                    found[k] = s[len(k) + 1:]
    return found


def parse_dfg_typed_value(raw_val):
    """Extract numeric/bool Python value from a KEYPATHVALUE 'TYPE:VALUE' string."""
    if raw_val is None or ':' not in raw_val:
        return None
    typ, val = raw_val.split(':', 1)
    typ = typ.strip().upper()
    val = val.strip()
    if typ == 'DOUBLE':
        try:
            return float(val)
        except ValueError:
            return None
    if typ in ('INT', 'UNSIGNED INT'):
        try:
            return int(val)
        except ValueError:
            return None
    if typ == 'BOOLEAN':
        return 1 if val.upper() == 'TRUE' else 0
    return val


def update_dfg(dfg_path, updates):
    """Update sibling .dfg file by {KEY: 'TYPE:VALUE'}, preserving line order & EOL."""
    if not os.path.exists(dfg_path):
        print(f"[WARN] 未找到同名 .dfg 文件 ({dfg_path})，跳过 Simulator 配置写入。")
        return
    with open(dfg_path, 'rb') as f:
        raw = f.read()
    newline = b'\r\n' if b'\r\n' in raw else b'\n'
    text = raw.decode('utf-8', errors='replace')
    lines = text.splitlines()

    hit_keys = set()
    new_lines = []
    for line in lines:
        replaced = False
        for k, typed_val in updates.items():
            if line.startswith(k + '='):
                old_val = line[len(k) + 1:]
                new_lines.append(f"{k}={typed_val}")
                print(f"[OK] .dfg {k}: {old_val} -> {typed_val}")
                hit_keys.add(k)
                replaced = True
                break
        if not replaced:
            new_lines.append(line)

    for k, typed_val in updates.items():
        if k not in hit_keys:
            new_lines.append(f"{k}={typed_val}")
            print(f"[OK] .dfg (新增) {k}={typed_val}")

    out_bytes = (newline.decode('ascii').join(new_lines) + newline.decode('ascii')).encode('utf-8')
    with open(dfg_path, 'wb') as f:
        f.write(out_bytes)


# ---------------------------------------------------------------- Inspect (--list)

def cmd_list(deck_path):
    deck_path, data_dir, h5_path, dfg_path = find_deck_files(deck_path)
    with h5py.File(deck_path, 'r') as fd:
        num_ts = int(fd.attrs.get('num timesteps', 1))
        file_ver = int(fd.attrs.get('current file version', 0))
    extra_h5 = [x for x in os.listdir(data_dir) if x.endswith('.h5') and x != '0.h5']

    with h5py.File(h5_path, 'r') as f:
        cd = f['CreatorData/0']
        print("======== EDEM Deck 参数一览 (v0.1.0) ========")
        ver_note = "已验证基线 ✅" if file_ver in TESTED_FILE_VERSIONS else "未实测版本 ⚠️ 写入前请保留备份并核对"
        print(f"[文件状态] file_version={file_ver} ({ver_note}) | num_timesteps={num_ts}, 历史分片数={len(extra_h5)} "
              f"{'(干净0秒态 ✅)' if num_ts <= 1 and len(extra_h5) == 0 else '(含已求解时间步 ⚠️ 改参后需重置或 Rewind)'}")

        # 运动学（列出全部类型，不限于 Linear Rotation）
        kins = list_kinematics(cd)
        print("\n[几何运动学]")
        if not kins:
            print("  (无运动学定义)")
        for gk, gname, kk, kname, ktype, kn in kins:
            gg_node = cd[f'GeometryGroups/{gk}']
            gmat = _dec(gg_node.attrs.get('material name', b''))
            a = kn.attrs
            en = int(a.get('enabled', 1))
            print(f"  GeometryGroups/{gk} (name='{gname}', material='{gmat}') / Kinematics/{kk} "
                  f"(name='{kname}', type='{ktype}', enabled={en})")
            if ktype == 'Linear Rotation':
                omega = float(a.get('initial angular velocity', 0.0))
                rpm = omega * 60.0 / (2.0 * math.pi)
                sp = kn['start point'][()]['xyz'] if 'start point' in kn else None
                ep = kn['end point'][()]['xyz'] if 'end point' in kn else None
                print(f"    initial angular velocity = {omega:.6f} rad/s ({rpm:.2f} r/min) | "
                      f"start_time = {a.get('start time')} s | end_time = {a.get('end time')} s")
                print(f"    axis = {sp} -> {ep}")
            else:
                skip = {'name', 'type', 'enabled'}
                extras = {k: (v.tolist() if isinstance(v, np.ndarray) else _dec(v) if isinstance(v, (bytes, np.bytes_)) else v)
                          for k, v in a.items() if k not in skip}
                print(f"    attrs = {extras}")

        # 颗粒类型
        print("\n[颗粒类型]")
        pt = cd['ParticleTypes']
        valid_pt_names = set()
        for pk in pt.keys():
            p = pt[pk]
            attrs = p.attrs
            pname = _dec(attrs.get('name', b''))
            mname = _dec(attrs.get('material name', b''))
            valid_pt_names.add(pname)
            spheres = p['spheres'][()]
            n_sp = len(spheres)
            r_min = float(np.min(spheres['physicalRadius'])) if n_sp else 0.0
            r_max = float(np.max(spheres['physicalRadius'])) if n_sp else 0.0
            r_str = (f"radius={r_min} m (d={r_min*2000:.2f} mm)"
                     if n_sp <= 1 or abs(r_max - r_min) < 1e-12
                     else f"radius={r_min}..{r_max} m (d={r_min*2000:.2f}..{r_max*2000:.2f} mm)")
            print(f"  ParticleTypes/{pk}: name='{pname}' material='{mname}' (spheres={n_sp})")
            print(f"    {r_str} | raw_mass={float(attrs.get('raw mass', 0)):.6f} kg "
                  f"| raw_vol={float(attrs.get('raw volume', 0)):.4e} m^3 | auto_props={attrs.get('auto properties')}")

        # 颗粒工厂
        print("\n[颗粒工厂]")
        seen_targets = []
        for gk, fk, fg in list_factories(cd):
            a = fg.attrs
            en = int(a.get('enabled', 1))
            unlim = int(a.get('unlimited', 0))
            is_mass = int(a.get('is mass creation', 0))
            tname = _dec(fg['FactType/type names'][()]) if 'FactType/type names' in fg else 'N/A'
            seen_targets.append((fk, tname, en))
            warn = " ⚠️ 绑定的颗粒类型不存在!" if tname not in valid_pt_names else ""
            print(f"  {fk} (GeometryGroups/{gk}): enabled={en}, target_particle='{tname}'{warn}")
            if is_mass:
                print(f"    mode=mass, mass_to_create={a.get('mass to create')} kg, "
                      f"creation_rate={a.get('creation rate')} kg/s, start_time={a.get('creation time')} s, unlimited={unlim}")
            else:
                print(f"    mode=count, num_to_create={a.get('num to create')}, "
                      f"creation_rate={a.get('creation rate')} /s, start_time={a.get('creation time')} s, unlimited={unlim}")
        active_targets = [t for _, t, en in seen_targets if en == 1]
        if len(active_targets) >= 2 and len(set(active_targets)) < len(active_targets):
            print(f"  [⚠️ 严重警告] 多个启用工厂绑定了同一个颗粒类型 {active_targets}！请用 --factory-type 修正。")

        # 材料
        print("\n[材料]")
        mats = cd['Materials'][()]
        for m in mats:
            print(f"  {_dec(m['name'])}: density={m['density']} kg/m^3, "
                  f"shearMod={m['shearMod']:.3e} Pa, poisson={m['poisson']}")

        # 接触参数
        print("\n[接触参数] (restitution, staticFriction, rollingFriction)")
        inter = cd['Interactions'][()]
        for i in inter:
            print(f"  {_dec(i['key'])}: {i['restitution']}, {i['staticFriction']}, {i['rollingFriction']}")

        # 仿真域与周期边界
        d = cd['Domain'].attrs
        xp = int(d.get('domain xPeriodic', 0))
        yp = int(d.get('domain yPeriodic', 0))
        zp = int(d.get('domain zPeriodic', 0))
        print("\n[仿真域与周期边界]")
        print(f"  domain min = {d.get('domain min')} | domain max = {d.get('domain max')}")
        print(f"  periodic (X, Y, Z) = ({xp}, {yp}, {zp})"
              f"{'  (注意: 有周期边界开启)' if (xp or yp or zp) else ''}")

        # 重力与接触模型
        print(f"\n[重力] gravity = {cd.attrs.get('gravity')}")
        print("\n[接触模型启用状态]")
        pm = cd['PhysicsModels']
        for cat in pm.keys():
            o = pm[cat]['order'][()]
            names = [_dec(x) for x in o]
            extra_info = ""
            if 'Standard Rolling Friction' in pm[cat]:
                rf_attr = pm[cat]['Standard Rolling Friction'].attrs.get('enable rolling friction', None)
                if rf_attr is not None:
                    extra_info = f" (enable rolling friction attr={int(rf_attr)})"
            print(f"  {cat}: order = {names}{extra_info}")

    # .dfg Simulator 参数
    dfg_info = read_dfg_summary(dfg_path)
    print("\n[Simulator 求解配置 (.dfg)]")
    if dfg_info is None:
        print("  (未找到同名 .dfg 文件)")
    else:
        if '!file-version' in dfg_info:
            print(f"  dfg file-version = {dfg_info['!file-version']}")
        for k in DFG_KEYS_INTEREST:
            if k in dfg_info:
                print(f"  {k.replace('PROCESSOR:', '')} = {dfg_info[k]}")


def export_template_csv(deck_path, out_csv):
    """Inspect a deck and export a generic starter CSV matrix for edem_batch.py."""
    deck_path, _, h5_path, dfg_path = find_deck_files(deck_path)
    cols = ['case_id', 'case_type']
    row = {'case_id': 'BASE', 'case_type': 'baseline'}

    with h5py.File(h5_path, 'r') as f:
        cd = f['CreatorData/0']

        # Rotating geometries
        rotations = [it for it in list_kinematics(cd) if it[4] == 'Linear Rotation']
        for _, gname, _, _, _, kn in rotations:
            omega = float(kn.attrs.get('initial angular velocity', 0.0))
            rpm = round(abs(omega) * 60.0 / (2.0 * math.pi), 4)
            col = f"rpm:{gname}" if len(rotations) > 1 else "rpm"
            cols.append(col)
            row[col] = rpm

        # Particle types
        pt = cd['ParticleTypes']
        for pk in pt.keys():
            p = pt[pk]
            pname = _dec(p.attrs.get('name', b''))
            spheres = p['spheres'][()]
            if len(spheres) == 1:
                col = f"d_mm:{pname}"
                cols.append(col)
                row[col] = round(float(spheres['physicalRadius'][0]) * 2000.0, 4)
            elif len(spheres) > 1:
                col = f"scale:{pname}"
                cols.append(col)
                row[col] = 1.0

        # Factories
        for _, fk, fg in list_factories(cd):
            en = int(fg.attrs.get('enabled', 1))
            is_mass = int(fg.attrs.get('is mass creation', 0))
            if is_mass:
                col = f"mass_kg:{fk}"
                val = float(fg.attrs.get('mass to create', 0.0)) if en else 0.0
            else:
                col = f"count:{fk}"
                val = int(fg.attrs.get('num to create', 0)) if en else 0
            cols.append(col)
            row[col] = val

        # Interactions (first 3 pairs as comments/columns if present, or all pairs)
        for inter in cd['Interactions'][()]:
            key = _dec(inter['key'])
            col = f"interaction:{key}"
            cols.append(col)
            row[col] = f"{inter['restitution']},{inter['staticFriction']},{inter['rollingFriction']}"

    dfg = read_dfg_summary(dfg_path) or {}
    dt_val = parse_dfg_typed_value(dfg.get('PROCESSOR:GENERAL:FIXED TIMESTEP'))
    sim_val = parse_dfg_typed_value(dfg.get('PROCESSOR:WRITE:TOTAL'))
    cols.extend(['timestep_s', 'sim_time_s'])
    row['timestep_s'] = f"{dt_val:.6e}" if dt_val is not None else ''
    row['sim_time_s'] = sim_val if sim_val is not None else ''

    out_dir = os.path.dirname(os.path.abspath(out_csv))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_csv, 'w', encoding='utf-8-sig', newline='') as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        w.writerow(row)
    print(f"[OK] 已根据 deck 导出通用参数矩阵模板: {out_csv} ({len(cols)} 列)")


# ---------------------------------------------------------------- Mutation functions

def set_speed_rad(creator, value, geom_filter=None):
    gk, kk = find_rotating_geometry(creator, geom_filter=geom_filter)
    if gk is None:
        sys.exit("[ERROR] 找不到旋转几何（没有 Linear Rotation 运动学）。")
    node = creator[f'GeometryGroups/{gk}/Kinematics/{kk}']
    gname = _dec(creator[f'GeometryGroups/{gk}'].attrs.get('name', b''))
    old = float(node.attrs['initial angular velocity'])
    node.attrs['initial angular velocity'] = np.float64(value)
    rpm = float(value) * 60.0 / (2.0 * math.pi)
    print(f"[OK] 几何 '{gname}' 转速 {old:.6f} -> {float(value):.6f} rad/s ({rpm:.2f} r/min)")


def set_speed_rpm(creator, rpm_val, clockwise=False, geom_filter=None):
    gk, kk = find_rotating_geometry(creator, geom_filter=geom_filter)
    if gk is None:
        sys.exit("[ERROR] 找不到旋转几何（没有 Linear Rotation 运动学）。")
    node = creator[f'GeometryGroups/{gk}/Kinematics/{kk}']
    gname = _dec(creator[f'GeometryGroups/{gk}'].attrs.get('name', b''))
    old = float(node.attrs['initial angular velocity'])
    mag = abs(float(rpm_val)) * 2.0 * math.pi / 60.0

    if float(rpm_val) < 0:
        sign = -1.0
    elif clockwise:
        dz = 1.0
        if 'start point' in node and 'end point' in node:
            dz = float(node['end point'][()]['xyz'][2] - node['start point'][()]['xyz'][2])
        sign = -1.0 if dz >= 0 else 1.0
    else:
        sign = -1.0 if old < 0 else 1.0

    new_omega = sign * mag
    node.attrs['initial angular velocity'] = np.float64(new_omega)
    print(f"[OK] 几何 '{gname}' 转速 {old:.6f} -> {new_omega:.6f} rad/s (目标 {rpm_val} r/min"
          f"{', +Z正视顺时针' if clockwise else ''})")


def set_kinematic_attr(creator, specs):
    """Generic kinematic attribute editor: specs=['Geom[:Kin]:attr=val', ...]."""
    kins = list_kinematics(creator)
    for spec in specs:
        lhs, sep, val_str = spec.partition('=')
        if not sep or ':' not in lhs:
            sys.exit(f"[ERROR] --kinematic-attr 格式应为 '几何名:属性名=值' 或 '几何名/运动名:属性名=值'，当前: {spec!r}")
        target, attr_name = lhs.rsplit(':', 1)
        target, attr_name, val_str = target.strip(), attr_name.strip(), val_str.strip()
        hits = [
            it for it in kins
            if _match_name(target, it[0], it[1], it[3], f"{it[1]}/{it[3]}")
        ]
        if not hits:
            avail = [f"{it[1]}/{it[3]} ({it[4]})" for it in kins]
            sys.exit(f"[ERROR] 找不到匹配 '{target}' 的运动学。可用: {avail}")
        if len(hits) > 1:
            avail = [f"{it[1]}/{it[3]} ({it[4]})" for it in hits]
            sys.exit(f"[ERROR] '{target}' 匹配到多个运动学: {avail}，请用 '几何名/运动名'。")
        _, gname, _, kname, _, kn = hits[0]
        if attr_name not in kn.attrs:
            sys.exit(f"[ERROR] 运动学 '{gname}/{kname}' 不含属性 '{attr_name}'。可用属性: {sorted(kn.attrs.keys())}")
        old = kn.attrs[attr_name]
        if isinstance(old, np.ndarray):
            parts = [float(x) for x in val_str.split(',')]
            if len(parts) != old.size:
                sys.exit(f"[ERROR] 属性 '{attr_name}' 需要 {old.size} 个分量，当前收到 {len(parts)} 个。")
            new_val = np.array(parts, dtype=old.dtype).reshape(old.shape)
        elif isinstance(old, (np.uint8, np.int32, np.int64, np.uint32, int)):
            new_val = type(old)(int(float(val_str)))
        elif isinstance(old, (np.floating, float)):
            new_val = type(old)(float(val_str))
        elif isinstance(old, (bytes, np.bytes_)):
            new_val = np.bytes_(val_str.encode('utf-8'))
        else:
            new_val = val_str
        kn.attrs[attr_name] = new_val
        print(f"[OK] 运动学 '{gname}/{kname}' @{attr_name}: {old} -> {new_val}")


def set_ball_count(creator, specs, fill_duration=None):
    """specs: ['工厂名=数量', ...]; 0 safely disables the factory (enabled=0)."""
    factories = {fk: (gk, fg) for gk, fk, fg in list_factories(creator)}
    for spec in specs:
        name, sep, num_str = spec.partition('=')
        name = name.strip()
        if not sep or name not in factories:
            sys.exit(f"[ERROR] 找不到工厂 '{name}'。可用工厂: {list(factories)}")
        num = int(round(float(num_str)))
        _, node = factories[name]
        old_num = int(node.attrs.get('num to create', 0))
        old_en = int(node.attrs.get('enabled', 1))
        if num <= 0:
            node.attrs['enabled'] = np.uint8(0)
            node.attrs['unlimited'] = np.uint8(0)
            print(f"[OK] 工厂 '{name}' 设 0 颗 -> 安全停用 (enabled: {old_en} -> 0, 保留原 num={old_num})")
        else:
            node.attrs['enabled'] = np.uint8(1)
            node.attrs['unlimited'] = np.uint8(0)
            node.attrs['is mass creation'] = np.uint8(0)
            node.attrs['num to create'] = np.int32(num)
            rate_msg = ""
            if fill_duration is not None and fill_duration > 0:
                new_rate = float(max(1.0, math.ceil(num / float(fill_duration))))
                node.attrs['creation rate'] = np.float64(new_rate)
                rate_msg = f", creation_rate -> {new_rate:.0f} /s"
            print(f"[OK] 工厂 '{name}' enabled=1, 数量 {old_num} -> {num}{rate_msg}")


def set_factory_mass(creator, specs, fill_duration=None):
    """specs: ['工厂名=质量kg', ...]; 0 safely disables the factory (enabled=0)."""
    factories = {fk: (gk, fg) for gk, fk, fg in list_factories(creator)}
    for spec in specs:
        name, sep, mass_str = spec.partition('=')
        name = name.strip()
        if not sep or name not in factories:
            sys.exit(f"[ERROR] 找不到工厂 '{name}'。可用工厂: {list(factories)}")
        mass = float(mass_str)
        _, node = factories[name]
        old_mass = float(node.attrs.get('mass to create', 0.0))
        old_en = int(node.attrs.get('enabled', 1))
        if mass <= 0:
            node.attrs['enabled'] = np.uint8(0)
            node.attrs['unlimited'] = np.uint8(0)
            print(f"[OK] 工厂 '{name}' 设 0 kg -> 安全停用 (enabled: {old_en} -> 0, 保留原 mass={old_mass})")
        else:
            node.attrs['enabled'] = np.uint8(1)
            node.attrs['unlimited'] = np.uint8(0)
            node.attrs['is mass creation'] = np.uint8(1)
            node.attrs['mass to create'] = np.float64(mass)
            rate_msg = ""
            if fill_duration is not None and fill_duration > 0:
                new_rate = float(mass / float(fill_duration))
                node.attrs['creation rate'] = np.float64(new_rate)
                rate_msg = f", creation_rate -> {new_rate:.4g} kg/s"
            print(f"[OK] 工厂 '{name}' enabled=1, 质量 {old_mass} -> {mass} kg{rate_msg}")


def set_factory_rate(creator, specs):
    """specs: ['工厂名=速率', ...] (particles/s or kg/s depending on factory mode)."""
    factories = {fk: (gk, fg) for gk, fk, fg in list_factories(creator)}
    for spec in specs:
        name, sep, rate_str = spec.partition('=')
        name = name.strip()
        if not sep or name not in factories:
            sys.exit(f"[ERROR] 找不到工厂 '{name}'。可用工厂: {list(factories)}")
        rate = float(rate_str)
        if rate <= 0:
            sys.exit(f"[ERROR] 工厂 '{name}' creation rate 必须大于 0，当前: {rate}")
        _, node = factories[name]
        old_r = float(node.attrs.get('creation rate', 0.0))
        node.attrs['creation rate'] = np.float64(rate)
        print(f"[OK] 工厂 '{name}' creation_rate: {old_r} -> {rate}")


def set_factory_type(creator, specs):
    """Update factory target particle binding: specs=['Steel_factory=Steel_Ball', ...]."""
    pt = creator['ParticleTypes']
    valid_names = {_dec(pt[pk].attrs.get('name', b'')) for pk in pt.keys()}
    factories = {fk: (gk, fg) for gk, fk, fg in list_factories(creator)}
    for spec in specs:
        fname, _, pname = spec.partition('=')
        fname, pname = fname.strip(), pname.strip()
        if fname not in factories:
            sys.exit(f"[ERROR] 找不到工厂 '{fname}'。可用工厂: {list(factories)}")
        if pname not in valid_names:
            sys.exit(f"[ERROR] 找不到颗粒名称 '{pname}'。可用颗粒名: {sorted(valid_names)}")
        _, fg = factories[fname]
        old_t = _dec(fg['FactType/type names'][()])
        del fg['FactType/type names']
        dt_ascii = h5py.string_dtype(encoding='ascii', length=None)
        fg['FactType'].create_dataset('type names', data=pname.encode('ascii'), dtype=dt_ascii)

        fs = fg['FactSize']
        for old_k in list(fs.keys()):
            if old_k != pname:
                del fs[old_k]
        if pname not in fs:
            g = fs.create_group(pname)
            g.attrs['type'] = np.bytes_(b'fixed')
        print(f"[OK] 工厂 '{fname}' 绑定颗粒类型 '{old_t}' -> '{pname}'")


def _sync_single_sphere_props(p_group, radius_m, density):
    vol = (4.0 / 3.0) * math.pi * (radius_m ** 3)
    mass = density * vol
    moi = 0.4 * mass * (radius_m ** 2)
    old_m = float(p_group.attrs.get('raw mass', 0.0))
    p_group.attrs['raw volume'] = np.float64(vol)
    p_group.attrs['raw mass'] = np.float64(mass)
    p_group.attrs['raw inertia'] = np.array([moi, moi, moi], dtype=np.float64)
    return old_m, mass, moi


def _iter_matching_particles(creator, target_filter=None):
    pt = creator['ParticleTypes']
    matched = []
    for pk in pt.keys():
        p = pt[pk]
        pname = _dec(p.attrs.get('name', b''))
        mname = _dec(p.attrs.get('material name', b''))
        if target_filter:
            tf = target_filter.strip().lower()
            if tf != mname.lower() and tf != pname.lower() and tf != pk.lower():
                continue
        matched.append((pk, pname, mname, p))
    if not matched:
        avail = [f"{_dec(pt[k].attrs.get('name', b''))} (material={_dec(pt[k].attrs.get('material name', b''))})"
                 for k in pt.keys()]
        sys.exit(f"[ERROR] 没有匹配 '{target_filter}' 的颗粒类型。可用: {avail}")
    return matched


def set_radius(creator, radius_m, target_filter=None):
    """Set particle radius (m).

    - Single-sphere: sets physical/contact radius and analytically recalculates
      raw volume, raw mass, and raw inertia from material density.
    - Multi-sphere: scales all child sphere radii and local pos proportionally by
      (radius_m / max_old_radius) and advises solving with --particle-properties.
    """
    r_new = float(radius_m)
    if r_new <= 0:
        sys.exit(f"[ERROR] 颗粒半径必须大于 0，当前输入: {r_new}")
    dens_map = get_material_densities(creator)
    for _, pname, mname, p in _iter_matching_particles(creator, target_filter):
        spheres = p['spheres']
        data = spheres[()]
        p.attrs['auto properties'] = np.uint8(1)
        p.attrs['calc props'] = np.uint8(1)
        if len(data) == 1:
            old_r = float(data[0]['physicalRadius'])
            data['physicalRadius'][:] = r_new
            data['contactRadius'][:] = r_new
            spheres[...] = data
            if mname in dens_map:
                old_m, mass, moi = _sync_single_sphere_props(p, r_new, dens_map[mname])
                print(f"[OK] 颗粒 '{pname}' (material={mname}): 半径 {old_r} -> {r_new} m (d={r_new*2000:.2f} mm), "
                      f"质量 {old_m*1e3:.2f} -> {mass*1e3:.2f} g, 惯量 -> {moi:.4e} kg·m^2")
            else:
                print(f"[OK] 颗粒 '{pname}' ({mname}): 半径 {old_r} -> {r_new} m "
                      f"(未找到材料密度，求解时请加 --particle-properties)")
        else:
            old_ref = float(np.max(data['physicalRadius']))
            if old_ref <= 0:
                sys.exit(f"[ERROR] 多球颗粒 '{pname}' 原半径 <= 0，无法等比缩放。")
            ratio = r_new / old_ref
            data['physicalRadius'] = data['physicalRadius'] * ratio
            data['contactRadius'] = data['contactRadius'] * ratio
            if 'pos' in data.dtype.names:
                data['pos'] = data['pos'] * ratio
            spheres[...] = data
            print(f"[OK] 多球颗粒 '{pname}' ({len(data)} spheres): 按比例 {ratio:.4f}x 等比缩放 "
                  f"(max_r {old_ref} -> {r_new} m, 子球 pos 已同步缩放; 求解时请务必加 --particle-properties)")


def set_scale(creator, factor, target_filter=None):
    """Uniformly scale single-sphere or multi-sphere particle size by factor (>0)."""
    f_val = float(factor)
    if f_val <= 0:
        sys.exit(f"[ERROR] 缩放因子必须大于 0，当前输入: {f_val}")
    dens_map = get_material_densities(creator)
    for _, pname, mname, p in _iter_matching_particles(creator, target_filter):
        spheres = p['spheres']
        data = spheres[()]
        old_r0 = float(data[0]['physicalRadius'])
        data['physicalRadius'] = data['physicalRadius'] * f_val
        data['contactRadius'] = data['contactRadius'] * f_val
        if 'pos' in data.dtype.names:
            data['pos'] = data['pos'] * f_val
        spheres[...] = data
        p.attrs['auto properties'] = np.uint8(1)
        p.attrs['calc props'] = np.uint8(1)
        if len(data) == 1 and mname in dens_map:
            r_new = float(data[0]['physicalRadius'])
            old_m, mass, moi = _sync_single_sphere_props(p, r_new, dens_map[mname])
            print(f"[OK] 颗粒 '{pname}' 缩放 {f_val}x: 半径 {old_r0} -> {r_new} m, "
                  f"质量 {old_m*1e3:.2f} -> {mass*1e3:.2f} g, 惯量 -> {moi:.4e} kg·m^2")
        else:
            print(f"[OK] 多球颗粒 '{pname}' ({len(data)} spheres) 缩放 {f_val}x "
                  f"(含 pos; 求解时请加 --particle-properties)")


def set_materials(creator, specs, field):
    """specs: ['材料名=值', ...], field: density/shearMod/poisson.

    When density is updated, also recalculates raw mass & raw inertia for single-sphere
    particles using that material so HDF5 metadata stays consistent.
    """
    mats = creator['Materials']
    data = mats[()]
    names = [_dec(m['name']) for m in data]
    updated_densities = {}
    for spec in specs:
        name, sep, val = spec.partition('=')
        name = name.strip()
        if not sep or name not in names:
            sys.exit(f"[ERROR] 找不到材料 '{name}'。可用材料: {names}")
        idx = names.index(name)
        old = data[idx][field]
        new_v = float(val)
        data[field][idx] = new_v
        print(f"[OK] 材料 '{name}' {field}: {old} -> {new_v}")
        if field == 'density':
            updated_densities[name] = new_v
    mats[...] = data

    if updated_densities and 'ParticleTypes' in creator:
        pt = creator['ParticleTypes']
        for pk in pt.keys():
            p = pt[pk]
            mname = _dec(p.attrs.get('material name', b''))
            if mname not in updated_densities:
                continue
            pname = _dec(p.attrs.get('name', b''))
            spheres = p['spheres'][()]
            p.attrs['auto properties'] = np.uint8(1)
            p.attrs['calc props'] = np.uint8(1)
            if len(spheres) == 1:
                r = float(spheres[0]['physicalRadius'])
                old_m, mass, moi = _sync_single_sphere_props(p, r, updated_densities[mname])
                print(f"[SYNC] 材料 '{mname}' 密度变更 -> 单球颗粒 '{pname}' 质量 "
                      f"{old_m*1e3:.2f} -> {mass*1e3:.2f} g, 惯量 -> {moi:.4e} kg·m^2")
            else:
                print(f"[WARN] 材料 '{mname}' 密度变更影响多球颗粒 '{pname}'，求解时请加 --particle-properties")


def _find_interaction_idx(keys, query_key):
    rev_key = '~'.join(query_key.split('~')[::-1])
    for i, k in enumerate(keys):
        if k == query_key or k == rev_key or k.lower() == query_key.lower() or k.lower() == rev_key.lower():
            return i
    return None


def set_interactions(creator, specs):
    """specs: ['材料A~材料B=e,sf,rf', ...], bidirectional key matching."""
    inter = creator['Interactions']
    data = inter[()]
    keys = [_dec(row['key']) for row in data]
    for spec in specs:
        key, sep, vals = spec.partition('=')
        key = key.strip()
        if not sep:
            sys.exit(f"[ERROR] --interaction 格式应为 'A~B=e,sf,rf'，当前: {spec!r}")
        r, sf, rf = [float(x) for x in vals.split(',')]
        hit_idx = _find_interaction_idx(keys, key)
        if hit_idx is None:
            sys.exit(f"[ERROR] 找不到接触对 '{key}'。可用接触对: {keys}")
        data['restitution'][hit_idx] = r
        data['staticFriction'][hit_idx] = sf
        data['rollingFriction'][hit_idx] = rf
        print(f"[OK] 接触 '{keys[hit_idx]}' -> e={r}, sf={sf}, rf={rf}")
    inter[...] = data


def set_interaction_field(creator, specs, field):
    """Update a single interaction property ('restitution', 'staticFriction', or 'rollingFriction')."""
    inter = creator['Interactions']
    data = inter[()]
    keys = [_dec(row['key']) for row in data]
    for spec in specs:
        key, sep, val_s = spec.partition('=')
        key = key.strip()
        if not sep:
            sys.exit(f"[ERROR] 接触单项参数格式应为 'A~B=值'，当前: {spec!r}")
        hit_idx = _find_interaction_idx(keys, key)
        if hit_idx is None:
            sys.exit(f"[ERROR] 找不到接触对 '{key}'。可用接触对: {keys}")
        old_v = float(data[field][hit_idx])
        new_v = float(val_s)
        data[field][hit_idx] = new_v
        print(f"[OK] 接触 '{keys[hit_idx]}' {field}: {old_v} -> {new_v}")
    inter[...] = data


def set_domain(creator, min_v=None, max_v=None):
    d = creator['Domain'].attrs
    if min_v:
        d['domain min'] = np.array(min_v, dtype=np.float64)
        print(f"[OK] domain min -> {min_v}")
    if max_v:
        d['domain max'] = np.array(max_v, dtype=np.float64)
        print(f"[OK] domain max -> {max_v}")


def set_periodic(creator, specs):
    """specs: ['z=0', 'x=1', ...]."""
    d = creator['Domain'].attrs
    axis_map = {'x': 'domain xPeriodic', 'y': 'domain yPeriodic', 'z': 'domain zPeriodic'}
    for spec in specs:
        ax, sep, val_s = spec.partition('=')
        ax = ax.strip().lower()
        if not sep or ax not in axis_map:
            sys.exit(f"[ERROR] --periodic 格式须为 x/y/z=0|1，当前: '{spec}'")
        attr_name = axis_map[ax]
        nv = 1 if int(float(val_s)) != 0 else 0
        old = int(d.get(attr_name, 0))
        d[attr_name] = np.uint8(nv)
        print(f"[OK] 周期边界 {attr_name}: {old} -> {nv}")


def set_gravity(creator, vec):
    old = creator.attrs.get('gravity')
    creator.attrs['gravity'] = np.array(vec, dtype=np.float64)
    print(f"[OK] 重力 {list(old) if old is not None else None} -> {vec}")


def list_physics_models(creator):
    pm = creator['PhysicsModels']
    for cat in pm.keys():
        groups = [k for k in pm[cat].keys() if k != 'order']
        order = pm[cat]['order'][()]
        active = [_dec(x) for x in order]
        print(f"\n[{cat}]")
        print(f"  可用模型: {groups}")
        print(f"  启用顺序: {active}")


def set_model(creator, category, model_name, enable):
    pm = creator['PhysicsModels']
    if category not in pm:
        sys.exit(f"[ERROR] 类别 '{category}' 不存在。可选: {list(pm.keys())}")
    cat = pm[category]
    groups = [k for k in cat.keys() if k != 'order']
    hit = None
    for g in groups:
        if g == model_name or g.lower() == model_name.lower():
            hit = g
            break
    if hit is None:
        subs = [g for g in groups if model_name.lower() in g.lower()]
        if len(subs) == 1:
            hit = subs[0]
        elif len(subs) > 1:
            sys.exit(f"[ERROR] '{model_name}' 匹配到多个模型: {subs}，请写全名。")
    if hit is None:
        sys.exit(f"[ERROR] 类别 '{category}' 里没有模型 '{model_name}'。可用: {groups}")

    if 'enable rolling friction' in cat[hit].attrs:
        cat[hit].attrs['enable rolling friction'] = np.uint8(1 if enable else 0)

    dset = cat['order']
    order = [_dec(x) for x in dset[()]]
    new_order = list(order)
    if enable:
        if hit in new_order:
            print(f"[OK] '{hit}' 已在 {category} 启用顺序中（已同步内部启用属性）")
            return
        new_order.append(hit)
    else:
        if hit not in new_order:
            print(f"[SKIP] '{hit}' 本来就没启用: {new_order}")
            return
        new_order.remove(hit)

    dt_ascii = h5py.string_dtype(encoding='ascii', length=None)
    del cat['order']
    cat.create_dataset('order', data=np.array([x.encode('ascii') for x in new_order], dtype=dt_ascii))
    verify = [_dec(x) for x in cat['order'][()]]
    print(f"[OK] {category} 启用顺序 {order} -> {verify}")


def reset_deck_to_zero(deck_path, data_dir):
    """Remove solved timestep shards (1..N.h5) and reset .dem num timesteps to 1."""
    removed = 0
    for fn in os.listdir(data_dir):
        if fn.endswith('.h5') and fn != '0.h5':
            os.remove(os.path.join(data_dir, fn))
            removed += 1
    with h5py.File(deck_path, 'a') as fd:
        old_ts = int(fd.attrs.get('num timesteps', 1))
        fd.attrs['num timesteps'] = np.int32(1)
    print(f"[OK] 已重置为 0 秒初始态: 清理旧时间步分片 {removed} 个, num_timesteps {old_ts} -> 1")


# ---------------------------------------------------------------- CLI entrypoint

def main(argv=None):
    ap = argparse.ArgumentParser(description='EDEM deck 参数修改工具 v0.1.0（通用前处理 + .dfg 自动化）')
    ap.add_argument('deck', help='.dem 文件路径')
    ap.add_argument('--list', action='store_true', help='列出当前所有参数（含文件版本、全部运动学、周期边界与 .dfg）')
    ap.add_argument('--list-models', action='store_true', help='列出接触模型类别、可用模型与启用顺序')
    ap.add_argument('--export-template-csv', metavar='PATH',
                    help='按当前 deck 结构导出通用参数矩阵 CSV 模板（供 edem_batch.py 使用）')

    # Kinematics
    ap.add_argument('--geometry', metavar='NAME',
                    help='配合 --speed / --rpm，指定目标旋转几何名（多转动件时必填）')
    ap.add_argument('--speed', type=float, help='改旋转几何角速度 (rad/s)')
    ap.add_argument('--rpm', type=float, help='按工程转速 (r/min) 改旋转几何角速度')
    ap.add_argument('--clockwise', action='store_true',
                    help='配合 --rpm，强制按从 +Z 正视顺时针方向确定角速度正负号')
    ap.add_argument('--kinematic-attr', nargs='+', metavar='GEOM:ATTR=VAL',
                    help='通用运动学属性修改（适用于平动/振动/起止时间等），如 "Screen:amplitude=0.005"')

    # Factories & particles
    ap.add_argument('--ball-count', nargs='+', metavar='FAC=N',
                    help='按数量改工厂颗粒数（N=0 时自动设 enabled=0 停用工厂）')
    ap.add_argument('--factory-mass', nargs='+', metavar='FAC=KG',
                    help='按质量改工厂生成总质量 kg（0 时自动设 enabled=0 停用工厂）')
    ap.add_argument('--factory-rate', nargs='+', metavar='FAC=RATE',
                    help='直接改工厂生成速率（/s 或 kg/s）')
    ap.add_argument('--fill-duration', type=float, metavar='SEC',
                    help='配合 --ball-count / --factory-mass，按期望下料秒数自动折算 creation rate')
    ap.add_argument('--factory-type', nargs='+', metavar='FAC=PARTICLE',
                    help='修正工厂绑定的颗粒名称，如 "Steel_factory=Steel_Ball"')
    ap.add_argument('--radius', type=float, help='改颗粒半径 (m)，联动重算单球质量/惯量或等比缩放多球 pos')
    ap.add_argument('--diameter-mm', type=float, help='按颗粒直径 (mm) 改半径，联动重算体积/质量/惯量')
    ap.add_argument('--scale', type=float, help='按缩放系数（如 1.2）等比缩放单球或多球颗粒')
    ap.add_argument('--radius-material', metavar='NAME',
                    help='配合 --radius / --diameter-mm / --scale，指定颗粒名或材料名（不传则作用于全部颗粒）')

    # Materials & contacts
    ap.add_argument('--material-density', nargs='+', metavar='NAME=V', help='改材料密度 (kg/m^3)，自动联动单球质量惯量')
    ap.add_argument('--material-shear', nargs='+', metavar='NAME=V', help='改材料剪切模量 (Pa)')
    ap.add_argument('--material-poisson', nargs='+', metavar='NAME=V', help='改材料泊松比')
    ap.add_argument('--interaction', nargs='+', metavar='A~B=e,sf,rf', help='改接触三项参数（可一次传多对）')
    ap.add_argument('--interaction-e', nargs='+', metavar='A~B=V', help='仅改接触恢复系数 restitution')
    ap.add_argument('--interaction-sf', nargs='+', metavar='A~B=V', help='仅改接触静摩擦 staticFriction')
    ap.add_argument('--interaction-rf', nargs='+', metavar='A~B=V', help='仅改接触滚动摩擦 rollingFriction')

    # Domain, periodic, gravity, physics models
    ap.add_argument('--domain-min', metavar='x,y,z', help='改仿真域最小值')
    ap.add_argument('--domain-max', metavar='x,y,z', help='改仿真域最大值')
    ap.add_argument('--periodic', nargs='+', metavar='AXIS=0|1', help='改周期边界，如 --periodic z=0')
    ap.add_argument('--gravity', metavar='x,y,z', help='改重力方向，如 0,-9.81,0')
    ap.add_argument('--model-enable', nargs='+', metavar='类别=模型名', help='启用接触模型')
    ap.add_argument('--model-disable', nargs='+', metavar='类别=模型名', help='停用接触模型')

    # Simulator (.dfg) & reset
    ap.add_argument('--timestep', type=float, metavar='SEC', help='改 .dfg 固定时间步长 (s)')
    ap.add_argument('--cell-size', type=float, metavar='M', help='改 .dfg 搜索网格绝对尺寸 (m)')
    ap.add_argument('--track-collisions', type=int, choices=[0, 1], help='改 .dfg Track Collisions 开关 (1=开, 0=关)')
    ap.add_argument('--save-interval', type=float, metavar='SEC', help='改 .dfg 数据保存时间间隔 (s)')
    ap.add_argument('--sim-time', type=float, metavar='SEC', help='改 .dfg 总仿真目标时长 (s)')
    ap.add_argument('--threads', type=int, metavar='N', help='改 .dfg CPU 求解线程数')
    ap.add_argument('--reset-zero', action='store_true', help='清理已求解的 1..N.h5 分片并将 .dem 重置回 0 秒态')
    ap.add_argument('--no-backup', action='store_true', help='不自动备份')
    args = ap.parse_args(argv)

    if args.speed is not None and args.rpm is not None:
        sys.exit("[ERROR] --speed 与 --rpm 不能同时传入，请只选其一。")
    size_flags = sum(x is not None for x in (args.radius, args.diameter_mm, args.scale))
    if size_flags > 1:
        sys.exit("[ERROR] --radius / --diameter-mm / --scale 每次只能指定一个。")

    deck_path, data_dir, h5_path, dfg_path = find_deck_files(args.deck)

    if args.list:
        cmd_list(deck_path)
        return

    if args.list_models:
        with h5py.File(h5_path, 'r') as f:
            list_physics_models(f['CreatorData/0'])
        return

    if args.export_template_csv:
        export_template_csv(deck_path, args.export_template_csv)
        return

    if not args.no_backup:
        backup(deck_path, h5_path, dfg_path)

    if args.reset_zero:
        reset_deck_to_zero(deck_path, data_dir)

    with h5py.File(h5_path, 'a') as f:
        cd = f['CreatorData/0']
        if args.speed is not None:
            set_speed_rad(cd, args.speed, geom_filter=args.geometry)
        if args.rpm is not None:
            set_speed_rpm(cd, args.rpm, clockwise=args.clockwise, geom_filter=args.geometry)
        if args.kinematic_attr:
            set_kinematic_attr(cd, args.kinematic_attr)
        if args.factory_type:
            set_factory_type(cd, args.factory_type)
        if args.material_density:
            set_materials(cd, args.material_density, 'density')
        if args.material_shear:
            set_materials(cd, args.material_shear, 'shearMod')
        if args.material_poisson:
            set_materials(cd, args.material_poisson, 'poisson')
        if args.radius is not None:
            set_radius(cd, args.radius, args.radius_material)
        if args.diameter_mm is not None:
            set_radius(cd, args.diameter_mm / 2000.0, args.radius_material)
        if args.scale is not None:
            set_scale(cd, args.scale, args.radius_material)
        if args.ball_count:
            set_ball_count(cd, args.ball_count, fill_duration=args.fill_duration)
        if args.factory_mass:
            set_factory_mass(cd, args.factory_mass, fill_duration=args.fill_duration)
        if args.factory_rate:
            set_factory_rate(cd, args.factory_rate)
        if args.interaction:
            set_interactions(cd, args.interaction)
        if args.interaction_e:
            set_interaction_field(cd, args.interaction_e, 'restitution')
        if args.interaction_sf:
            set_interaction_field(cd, args.interaction_sf, 'staticFriction')
        if args.interaction_rf:
            set_interaction_field(cd, args.interaction_rf, 'rollingFriction')
        if args.domain_min or args.domain_max:
            mn = [float(x) for x in args.domain_min.split(',')] if args.domain_min else None
            mx = [float(x) for x in args.domain_max.split(',')] if args.domain_max else None
            set_domain(cd, mn, mx)
        if args.periodic:
            set_periodic(cd, args.periodic)
        if args.gravity:
            vec = [float(x) for x in args.gravity.split(',')]
            if len(vec) != 3:
                sys.exit("[ERROR] --gravity 需要 x,y,z 三个数，如 0,-9.81,0")
            set_gravity(cd, vec)
        if args.model_enable:
            for item in args.model_enable:
                cat, _, name = item.partition('=')
                set_model(cd, cat.strip(), name.strip(), True)
        if args.model_disable:
            for item in args.model_disable:
                cat, _, name = item.partition('=')
                set_model(cd, cat.strip(), name.strip(), False)

    dfg_updates = {}
    if args.timestep is not None:
        dfg_updates['PROCESSOR:GENERAL:AUTO TIMESTEP'] = 'BOOLEAN:FALSE'
        dfg_updates['PROCESSOR:GENERAL:FIXED TIMESTEP'] = f'DOUBLE:{args.timestep:.6e}'
    if args.cell_size is not None:
        dfg_updates['PROCESSOR:GENERAL:AUTO GRIDCELL'] = 'BOOLEAN:FALSE'
        dfg_updates['PROCESSOR:GENERAL:GRID CELL SIZE'] = f'DOUBLE:{args.cell_size:.6g}'
    if args.track_collisions is not None:
        dfg_updates['PROCESSOR:GENERAL:TRACK COLLISIONS'] = 'BOOLEAN:TRUE' if args.track_collisions else 'BOOLEAN:FALSE'
    if args.save_interval is not None:
        dfg_updates['PROCESSOR:WRITE:SELECTIVE SAVE'] = 'BOOLEAN:FALSE'
        dfg_updates['PROCESSOR:WRITE:INTERVAL'] = f'DOUBLE:{args.save_interval:.6g}'
    if args.sim_time is not None:
        dfg_updates['PROCESSOR:WRITE:TOTAL'] = f'DOUBLE:{args.sim_time:.6g}'
    if args.threads is not None:
        dfg_updates['PROCESSOR:GENERAL:NUM THREADS'] = f'INT:{args.threads}'
    if dfg_updates:
        update_dfg(dfg_path, dfg_updates)

    print("\n[DONE] 修改完成。建议跑一次 --list 核对回读结果。")


if __name__ == '__main__':
    main()
