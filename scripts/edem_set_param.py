#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
EDEM Deck 参数修改工具 v2.0（前处理 + Simulator .dfg 自动化）

直接读写 EDEM 2022.1 deck 文件（.dem + _data/0.h5 + .dfg），绕过 GUI 手工逐项点击，
也绕过旧版 edempy（0.2.0）缺失的前处理 API 与 numpy 2.x 兼容性崩溃。

v2.0 升级要点（基于真实球磨机 deck 实测）：
1. 半径-体积-质量-转动惯量严格联动：改球径（--radius 或 --diameter-mm）时自动查材料密度，
   同步重算 raw volume / raw mass / raw inertia 并置位 auto properties=1。
2. 周期边界读写：--list 显示 domain x/y/zPeriodic，新增 --periodic z=0 一键启停。
3. 单介质 0 颗球安全停用与落料时长联动：--ball-count 传 0 时自动设工厂 enabled=0（防报错/无限生成），
   传正数自动设 enabled=1；支持 --fill-duration 统一控制落料完成时长；支持 --factory-type 修正工厂绑定的颗粒名。
4. 多参数单次批改：--interaction / --material-* / --model-* 均支持一次传多项，只做一次备份。
5. 工程单位防呆：新增 --rpm（自动换算 rad/s，支持 --clockwise 对标顺时针坐标系）与 --diameter-mm。
6. 打通同名 .dfg 求解器配置读写：--list 同步显示时间步、网格、Track Collisions、时长与保存间隔，
   支持 --timestep / --cell-size / --track-collisions / --save-interval / --sim-time / --threads，
   以及 --reset-zero 清理已跑过的旧时间步还原为 0 秒初始态。
"""
import argparse
import datetime
import math
import os
import shutil
import sys

import h5py
import numpy as np


# ---------------------------------------------------------------- 基础路径与备份

def find_deck_files(deck_path):
    """由 .dem 路径推导 _data/0.h5 与同名 .dfg 路径。"""
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
    """修改前轻量备份：复制 .dem、0.h5 及 .dfg（若存在）。"""
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


def find_mill_geometry(creator):
    """定位带 Linear Rotation 运动学的磨机几何，返回 (geometry_group_key, kinematic_key)。"""
    gg = creator['GeometryGroups']
    for gk in gg.keys():
        kin = gg[gk].get('Kinematics')
        if kin is not None and len(kin) > 0:
            for kk in kin.keys():
                if kin[kk].attrs.get('type') == b'Linear Rotation':
                    return gk, kk
    return None, None


def list_factories(creator):
    """返回所有颗粒工厂 [(gk, fk, factory_group), ...]。"""
    gg = creator['GeometryGroups']
    result = []
    for gk in gg.keys():
        facs = gg[gk].get('Factories')
        if facs is not None:
            for fk in facs.keys():
                result.append((gk, fk, facs[fk]))
    return result


def get_material_densities(creator):
    """读取材料密度字典 {material_name_str: float_density}。"""
    mats = creator['Materials'][()]
    return {_dec(m['name']): float(m['density']) for m in mats}


# ---------------------------------------------------------------- .dfg 读写工具

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
    """读取 .dfg 中的关键 Simulator 求解参数。"""
    if not os.path.exists(dfg_path):
        return None
    found = {}
    with open(dfg_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            for k in DFG_KEYS_INTEREST:
                if s.startswith(k + '='):
                    found[k] = s[len(k) + 1:]
    return found


def update_dfg(dfg_path, updates):
    """按 {KEY: 'TYPE:VALUE'} 更新同名 .dfg 文件，保持原有行序与换行符。"""
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


# ---------------------------------------------------------------- 读取/列出 (--list)

def cmd_list(deck_path):
    deck_path, data_dir, h5_path, dfg_path = find_deck_files(deck_path)
    with h5py.File(deck_path, 'r') as fd:
        num_ts = int(fd.attrs.get('num timesteps', 1))
    extra_h5 = [x for x in os.listdir(data_dir) if x.endswith('.h5') and x != '0.h5']

    with h5py.File(h5_path, 'r') as f:
        cd = f['CreatorData/0']
        print("======== EDEM Deck 参数一览 (v2.0) ========")
        print(f"[文件状态] num_timesteps={num_ts}, 历史结果分片数={len(extra_h5)} "
              f"{'(干净0秒态 ✅)' if num_ts <= 1 and len(extra_h5) == 0 else '(含已求解时间步 ⚠️ 改参后需重置或 Rewind)'}")

        # 转速与转轴
        gk, kk = find_mill_geometry(cd)
        if gk is not None:
            gg_node = cd[f'GeometryGroups/{gk}']
            kin = gg_node[f'Kinematics/{kk}']
            a = kin.attrs
            omega = float(a.get('initial angular velocity', 0.0))
            rpm = omega * 60.0 / (2.0 * math.pi)
            sp = kin['start point'][()]['xyz'] if 'start point' in kin else None
            ep = kin['end point'][()]['xyz'] if 'end point' in kin else None
            print(f"\n[转速与运动学] GeometryGroups/{gk} (name={_dec(gg_node.attrs.get('name', b''))}, "
                  f"material={_dec(gg_node.attrs.get('material name', b''))})")
            print(f"  initial angular velocity = {omega:.6f} rad/s  ({rpm:.2f} r/min)")
            print(f"  start time = {a.get('start time')} s | axis = {sp} -> {ep}")

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
            r = float(spheres['physicalRadius'][0]) if len(spheres) else 0.0
            print(f"  ParticleTypes/{pk}: name='{pname}' material='{mname}' (spheres={len(spheres)})")
            print(f"    radius={r} m (d={r*2000:.2f} mm) | raw_mass={float(attrs.get('raw mass', 0)):.6f} kg "
                  f"| raw_vol={float(attrs.get('raw volume', 0)):.4e} m^3 | auto_props={attrs.get('auto properties')}")

        # 颗粒工厂
        print("\n[颗粒工厂]")
        seen_targets = []
        for gk, fk, fg in list_factories(cd):
            a = fg.attrs
            en = int(a.get('enabled', 1))
            unlim = int(a.get('unlimited', 0))
            tname = _dec(fg['FactType/type names'][()]) if 'FactType/type names' in fg else 'N/A'
            seen_targets.append((fk, tname, en))
            warn = " ⚠️ 绑定的颗粒类型不存在!" if tname not in valid_pt_names else ""
            print(f"  {fk} (GeometryGroups/{gk}): enabled={en}, target_particle='{tname}'{warn}")
            print(f"    num_to_create={a.get('num to create')}, creation_rate={a.get('creation rate')} /s, "
                  f"start_time={a.get('creation time')} s, unlimited={unlim}, is_mass={a.get('is mass creation')}")
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
        print(f"  periodic (X, Y, Z) = ({xp}, {yp}, {zp}) "
              f"{'⚠️ Z向周期边界当前开启!' if zp == 1 else ''}")

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
        for k in DFG_KEYS_INTEREST:
            if k in dfg_info:
                print(f"  {k.replace('PROCESSOR:', '')} = {dfg_info[k]}")


# ---------------------------------------------------------------- 修改函数

def set_speed_rad(creator, value):
    gk, kk = find_mill_geometry(creator)
    if gk is None:
        sys.exit("[ERROR] 找不到磨机几何（没有 Linear Rotation 运动学）。")
    node = creator[f'GeometryGroups/{gk}/Kinematics/{kk}']
    old = float(node.attrs['initial angular velocity'])
    node.attrs['initial angular velocity'] = np.float64(value)
    rpm = float(value) * 60.0 / (2.0 * math.pi)
    print(f"[OK] 转速 {old:.6f} -> {float(value):.6f} rad/s ({rpm:.2f} r/min)")


def set_speed_rpm(creator, rpm_val, clockwise=False):
    gk, kk = find_mill_geometry(creator)
    if gk is None:
        sys.exit("[ERROR] 找不到磨机几何（没有 Linear Rotation 运动学）。")
    node = creator[f'GeometryGroups/{gk}/Kinematics/{kk}']
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
    print(f"[OK] 转速 {old:.6f} -> {new_omega:.6f} rad/s (目标 {rpm_val} r/min"
          f"{', +Z正视顺时针' if clockwise else ''})")


def set_ball_count(creator, specs, fill_duration=None):
    """specs: ['工厂名=数量', ...]；数量为 0 时自动安全停用该工厂 (enabled=0)。"""
    factories = {fk: (gk, fg) for gk, fk, fg in list_factories(creator)}
    for spec in specs:
        name, _, num_str = spec.partition('=')
        name = name.strip()
        if name not in factories:
            sys.exit(f"[ERROR] 找不到工厂 '{name}'。可用工厂: {list(factories)}")
        num = int(num_str)
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


def set_factory_type(creator, specs):
    """修正工厂绑定的颗粒类型名：specs=['Steel_factory=Stell_Ball', ...]"""
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


def set_radius(creator, radius_m, target_filter=None):
    """修改颗粒半径（m），并严格联动重算单球体积、质量与转动惯量。"""
    r_new = float(radius_m)
    if r_new <= 0:
        sys.exit(f"[ERROR] 颗粒半径必须大于 0，当前输入: {r_new}")
    dens_map = get_material_densities(creator)
    pt = creator['ParticleTypes']
    changed = 0
    for pk in pt.keys():
        p = pt[pk]
        pname = _dec(p.attrs.get('name', b''))
        mname = _dec(p.attrs.get('material name', b''))
        if target_filter:
            tf = target_filter.lower()
            if tf != mname.lower() and tf != pname.lower():
                continue
        spheres = p['spheres']
        data = spheres[()]
        old_r = float(data[0]['physicalRadius'])
        data['physicalRadius'][:] = r_new
        data['contactRadius'][:] = r_new
        spheres[...] = data

        p.attrs['auto properties'] = np.uint8(1)
        p.attrs['calc props'] = np.uint8(1)
        if len(data) == 1 and mname in dens_map:
            rho = dens_map[mname]
            vol = (4.0 / 3.0) * math.pi * (r_new ** 3)
            mass = rho * vol
            moi = 0.4 * mass * (r_new ** 2)
            old_m = float(p.attrs.get('raw mass', 0.0))
            p.attrs['raw volume'] = np.float64(vol)
            p.attrs['raw mass'] = np.float64(mass)
            p.attrs['raw inertia'] = np.array([moi, moi, moi], dtype=np.float64)
            print(f"[OK] 颗粒 '{pname}' (material={mname}): 半径 {old_r} -> {r_new} m (d={r_new*2000:.2f} mm), "
                  f"质量 {old_m*1e3:.2f} -> {mass*1e3:.2f} g, 惯量 -> {moi:.4e} kg·m^2")
        else:
            print(f"[OK] 颗粒 '{pname}' ({mname}): 半径 {old_r} -> {r_new} m "
                  f"(多球或未知材料密度，求解时请加 --particle-properties 由 EDEM 重算质量)")
        changed += 1
    if changed == 0:
        sys.exit(f"[ERROR] 没有匹配 '{target_filter}' 的颗粒类型。")


def set_materials(creator, specs, field):
    """specs: ['材料名=值', ...], field: density/shearMod/poisson"""
    mats = creator['Materials']
    data = mats[()]
    names = [_dec(m['name']) for m in data]
    for spec in specs:
        name, _, val = spec.partition('=')
        name = name.strip()
        if name not in names:
            sys.exit(f"[ERROR] 找不到材料 '{name}'。可用材料: {names}")
        idx = names.index(name)
        old = data[idx][field]
        data[field][idx] = float(val)
        print(f"[OK] 材料 '{name}' {field}: {old} -> {float(val)}")
    mats[...] = data


def set_interactions(creator, specs):
    """specs: ['材料A~材料B=e,sf,rf', ...]，支持双向键名匹配。"""
    inter = creator['Interactions']
    data = inter[()]
    keys = [_dec(row['key']) for row in data]
    for spec in specs:
        key, _, vals = spec.partition('=')
        key = key.strip()
        r, sf, rf = [float(x) for x in vals.split(',')]
        rev_key = '~'.join(key.split('~')[::-1])
        hit_idx = None
        for i, k in enumerate(keys):
            if k == key or k == rev_key or k.lower() == key.lower() or k.lower() == rev_key.lower():
                hit_idx = i
                break
        if hit_idx is None:
            sys.exit(f"[ERROR] 找不到接触对 '{key}'。可用接触对: {keys}")
        data['restitution'][hit_idx] = r
        data['staticFriction'][hit_idx] = sf
        data['rollingFriction'][hit_idx] = rf
        print(f"[OK] 接触 '{keys[hit_idx]}' -> e={r}, sf={sf}, rf={rf}")
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
    """specs: ['z=0', 'x=0', ...]"""
    d = creator['Domain'].attrs
    axis_map = {'x': 'domain xPeriodic', 'y': 'domain yPeriodic', 'z': 'domain zPeriodic'}
    for spec in specs:
        ax, _, val_s = spec.partition('=')
        ax = ax.strip().lower()
        if ax not in axis_map:
            sys.exit(f"[ERROR] --periodic 轴名必须是 x/y/z，当前: '{ax}'")
        attr_name = axis_map[ax]
        nv = 1 if int(val_s) != 0 else 0
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
    """清理 _data 下除 0.h5 外的已求解时间步文件，并将 .dem 的 num timesteps 重置为 1。"""
    removed = 0
    for fn in os.listdir(data_dir):
        if fn.endswith('.h5') and fn != '0.h5':
            os.remove(os.path.join(data_dir, fn))
            removed += 1
    with h5py.File(deck_path, 'a') as fd:
        old_ts = int(fd.attrs.get('num timesteps', 1))
        fd.attrs['num timesteps'] = np.int32(1)
    print(f"[OK] 已重置为 0 秒初始态: 清理旧时间步分片 {removed} 个, num_timesteps {old_ts} -> 1")


# ---------------------------------------------------------------- 主入口

def main():
    ap = argparse.ArgumentParser(description='EDEM deck 参数修改工具 v2.0')
    ap.add_argument('deck', help='.dem 文件路径')
    ap.add_argument('--list', action='store_true', help='列出当前所有参数（含周期边界与 .dfg 求解设置）')
    ap.add_argument('--list-models', action='store_true', help='列出接触模型类别、可用模型与启用顺序')

    # 转速
    ap.add_argument('--speed', type=float, help='改磨机转速 (rad/s)')
    ap.add_argument('--rpm', type=float, help='按工程转速 (r/min) 改磨机角速度')
    ap.add_argument('--clockwise', action='store_true',
                    help='配合 --rpm，强制按从 +Z 正视顺时针方向确定角速度正负号')

    # 工厂与颗粒
    ap.add_argument('--ball-count', nargs='+', metavar='NAME=N',
                    help='改工厂颗粒数（N=0 时自动设 enabled=0 停用工厂）')
    ap.add_argument('--fill-duration', type=float, metavar='SEC',
                    help='配合 --ball-count，按期望落料完成秒数自动调整 creation rate')
    ap.add_argument('--factory-type', nargs='+', metavar='FAC=PARTICLE',
                    help='修正工厂绑定的颗粒名称，如 "Steel_factory=Stell_Ball"')
    ap.add_argument('--radius', type=float, help='改颗粒半径 (m)，联动重算体积/质量/转动惯量')
    ap.add_argument('--diameter-mm', type=float, help='按颗粒直径 (mm) 改半径，联动重算体积/质量/惯量')
    ap.add_argument('--radius-material', metavar='NAME',
                    help='配合 --radius 或 --diameter-mm，指定材料名或颗粒名（如 Ceramic 或 Ceramic_Ball）')

    # 材料与接触（均支持多项一次传入）
    ap.add_argument('--material-density', nargs='+', metavar='NAME=V', help='改材料密度 (kg/m^3)')
    ap.add_argument('--material-shear', nargs='+', metavar='NAME=V', help='改材料剪切模量 (Pa)')
    ap.add_argument('--material-poisson', nargs='+', metavar='NAME=V', help='改材料泊松比')
    ap.add_argument('--interaction', nargs='+', metavar='A~B=e,sf,rf', help='改接触参数（可一次传多对）')

    # 域、周期边界、重力、接触模型
    ap.add_argument('--domain-min', metavar='x,y,z', help='改仿真域最小值')
    ap.add_argument('--domain-max', metavar='x,y,z', help='改仿真域最大值')
    ap.add_argument('--periodic', nargs='+', metavar='AXIS=0|1', help='改周期边界，如 --periodic z=0')
    ap.add_argument('--gravity', metavar='x,y,z', help='改重力方向，如 0,-9.81,0')
    ap.add_argument('--model-enable', nargs='+', metavar='类别=模型名', help='启用接触模型')
    ap.add_argument('--model-disable', nargs='+', metavar='类别=模型名', help='停用接触模型')

    # Simulator (.dfg) 与重置
    ap.add_argument('--timestep', type=float, metavar='SEC', help='改 .dfg 固定时间步长 (s)')
    ap.add_argument('--cell-size', type=float, metavar='M', help='改 .dfg 搜索网格绝对尺寸 (m)')
    ap.add_argument('--track-collisions', type=int, choices=[0, 1], help='改 .dfg Track Collisions 开关 (1=开, 0=关)')
    ap.add_argument('--save-interval', type=float, metavar='SEC', help='改 .dfg 数据保存时间间隔 (s)')
    ap.add_argument('--sim-time', type=float, metavar='SEC', help='改 .dfg 总仿真目标时长 (s)')
    ap.add_argument('--threads', type=int, metavar='N', help='改 .dfg CPU 求解线程数')
    ap.add_argument('--reset-zero', action='store_true', help='清理已求解的 1..N.h5 分片并将 .dem 重置回 0 秒态')
    ap.add_argument('--no-backup', action='store_true', help='不自动备份')
    args = ap.parse_args()

    deck_path, data_dir, h5_path, dfg_path = find_deck_files(args.deck)

    if args.list:
        cmd_list(deck_path)
        return

    if args.list_models:
        with h5py.File(h5_path, 'r') as f:
            list_physics_models(f['CreatorData/0'])
        return

    if not args.no_backup:
        backup(deck_path, h5_path, dfg_path)

    if args.reset_zero:
        reset_deck_to_zero(deck_path, data_dir)

    with h5py.File(h5_path, 'a') as f:
        cd = f['CreatorData/0']
        if args.speed is not None:
            set_speed_rad(cd, args.speed)
        if args.rpm is not None:
            set_speed_rpm(cd, args.rpm, clockwise=args.clockwise)
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
        if args.ball_count:
            set_ball_count(cd, args.ball_count, fill_duration=args.fill_duration)
        if args.interaction:
            set_interactions(cd, args.interaction)
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
