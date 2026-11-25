#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Self-contained regression test suite for edem-deck-editor.

Builds minimal synthetic HDF5 (.dem + _data/0.h5) and .dfg decks matching the
verified Altair EDEM 2022.1 schema (no proprietary files or EDEM install needed).
"""
import csv
import math
import os
import sys
import tempfile
import unittest

import h5py
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, 'scripts')
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import edem_batch  # noqa: E402
import edem_set_param as esp  # noqa: E402


MAT_DTYPE = np.dtype([
    ('name', 'S256'),
    ('poisson', '<f8'),
    ('shearMod', '<f8'),
    ('density', '<f8'),
    ('workFunction', '<f8'),
    ('type', '<u4'),
    ('dbId', '<i4'),
])

INTER_DTYPE = np.dtype([
    ('key', 'S512'),
    ('restitution', '<f8'),
    ('staticFriction', '<f8'),
    ('rollingFriction', '<f8'),
])

SPHERE_DTYPE = np.dtype({
    'names': ['name', 'contactRadius', 'physicalRadius', 'pos'],
    'formats': ['S256', '<f8', '<f8', ('<f8', (3,))],
    'offsets': [0, 512, 520, 528],
    'itemsize': 552,
})

PT3_DTYPE = np.dtype([('xyz', '<f8', (3,))])


def create_synthetic_deck(base_dir, name='template', multi_geom=False, multi_sphere=False):
    """Create a synthetic EDEM 2022.1 deck (.dem, _data/0.h5, _data/1.h5, .dfg)."""
    dem_path = os.path.join(base_dir, f'{name}.dem')
    data_dir = os.path.join(base_dir, f'{name}_data')
    os.makedirs(data_dir, exist_ok=True)
    h5_path = os.path.join(data_dir, '0.h5')
    dfg_path = os.path.join(base_dir, f'{name}.dfg')

    with h5py.File(dem_path, 'w') as fd:
        fd.attrs['current file version'] = np.uint32(2622234)
        fd.attrs['original file version'] = np.uint32(2622234)
        fd.attrs['num timesteps'] = np.int32(5)

    # Dummy solved shard to verify reset/clean clone strips it
    with h5py.File(os.path.join(data_dir, '1.h5'), 'w') as f1:
        f1.attrs['dummy'] = 1

    dt_ascii = h5py.string_dtype(encoding='ascii', length=None)

    with h5py.File(h5_path, 'w') as f:
        cd = f.create_group('CreatorData/0')
        cd.attrs['gravity'] = np.array([0.0, -9.81, 0.0], dtype=np.float64)

        mats = np.zeros(3, dtype=MAT_DTYPE)
        mats[0] = (b'Steel', 0.30, 7.0e10, 7800.0, 0.0, 0, 1)
        mats[1] = (b'Ceramic', 0.25, 1.0e11, 3600.0, 0.0, 0, 2)
        mats[2] = (b'Wall', 0.30, 7.0e10, 7800.0, 0.0, 1, 3)
        cd.create_dataset('Materials', data=mats)

        inters = np.zeros(3, dtype=INTER_DTYPE)
        inters[0] = (b'Steel~Steel', 0.5, 0.5, 0.01)
        inters[1] = (b'Steel~Wall', 0.5, 0.4, 0.02)
        inters[2] = (b'Ceramic~Wall', 0.4, 0.3, 0.05)
        cd.create_dataset('Interactions', data=inters)

        dom = cd.create_group('Domain')
        dom.attrs['domain min'] = np.array([-0.5, -0.5, -0.5], dtype=np.float64)
        dom.attrs['domain max'] = np.array([0.5, 0.5, 0.5], dtype=np.float64)
        dom.attrs['domain xPeriodic'] = np.uint8(0)
        dom.attrs['domain yPeriodic'] = np.uint8(0)
        dom.attrs['domain zPeriodic'] = np.uint8(1)

        # Particles
        pts = cd.create_group('ParticleTypes')

        p0 = pts.create_group('0')
        p0.attrs['name'] = np.bytes_(b'Steel_Ball')
        p0.attrs['material name'] = np.bytes_(b'Steel')
        p0.attrs['auto properties'] = np.uint8(1)
        p0.attrs['calc props'] = np.uint8(1)
        s0 = np.zeros(1, dtype=SPHERE_DTYPE)
        s0[0] = (b'sphere 0', 0.01, 0.01, [0.0, 0.0, 0.0])
        p0.create_dataset('spheres', data=s0)
        esp._sync_single_sphere_props(p0, 0.01, 7800.0)

        p1 = pts.create_group('1')
        p1.attrs['name'] = np.bytes_(b'Ceramic_Ball')
        p1.attrs['material name'] = np.bytes_(b'Ceramic')
        p1.attrs['auto properties'] = np.uint8(1)
        p1.attrs['calc props'] = np.uint8(1)
        if multi_sphere:
            s1 = np.zeros(2, dtype=SPHERE_DTYPE)
            s1[0] = (b'sphere 0', 0.008, 0.008, [-0.005, 0.0, 0.0])
            s1[1] = (b'sphere 1', 0.006, 0.006, [0.005, 0.0, 0.0])
            p1.create_dataset('spheres', data=s1)
            p1.attrs['raw volume'] = np.float64(0.0)
            p1.attrs['raw mass'] = np.float64(0.0)
            p1.attrs['raw inertia'] = np.zeros(3, dtype=np.float64)
        else:
            s1 = np.zeros(1, dtype=SPHERE_DTYPE)
            s1[0] = (b'sphere 0', 0.008, 0.008, [0.0, 0.0, 0.0])
            p1.create_dataset('spheres', data=s1)
            esp._sync_single_sphere_props(p1, 0.008, 3600.0)

        # Geometries & kinematics
        ggs = cd.create_group('GeometryGroups')
        g0 = ggs.create_group('0')
        g0.attrs['name'] = np.bytes_(b'Drum')
        g0.attrs['material name'] = np.bytes_(b'Wall')
        k0 = g0.create_group('Kinematics/0')
        k0.attrs['name'] = np.bytes_(b'DrumRot')
        k0.attrs['type'] = np.bytes_(b'Linear Rotation')
        k0.attrs['enabled'] = np.uint8(1)
        k0.attrs['initial angular velocity'] = np.float64(2.0)
        k0.attrs['start time'] = np.float64(0.0)
        k0.attrs['end time'] = np.float64(9999.0)
        sp = np.zeros((), dtype=PT3_DTYPE)
        sp['xyz'] = [0.0, 0.0, 0.0]
        ep = np.zeros((), dtype=PT3_DTYPE)
        ep['xyz'] = [0.0, 0.0, 1.0]
        k0.create_dataset('start point', data=sp)
        k0.create_dataset('end point', data=ep)

        if multi_geom:
            g_imp = ggs.create_group('3')
            g_imp.attrs['name'] = np.bytes_(b'Impeller')
            g_imp.attrs['material name'] = np.bytes_(b'Wall')
            k_imp = g_imp.create_group('Kinematics/0')
            k_imp.attrs['name'] = np.bytes_(b'ImpRot')
            k_imp.attrs['type'] = np.bytes_(b'Linear Rotation')
            k_imp.attrs['enabled'] = np.uint8(1)
            k_imp.attrs['initial angular velocity'] = np.float64(5.0)
            k_imp.attrs['start time'] = np.float64(0.0)
            k_imp.attrs['end time'] = np.float64(9999.0)
            k_imp.create_dataset('start point', data=sp)
            k_imp.create_dataset('end point', data=ep)

            g_scr = ggs.create_group('4')
            g_scr.attrs['name'] = np.bytes_(b'Screen')
            g_scr.attrs['material name'] = np.bytes_(b'Wall')
            k_scr = g_scr.create_group('Kinematics/0')
            k_scr.attrs['name'] = np.bytes_(b'Vib')
            k_scr.attrs['type'] = np.bytes_(b'Sinusoidal Translation')
            k_scr.attrs['enabled'] = np.uint8(1)
            k_scr.attrs['amplitude'] = np.float64(0.002)
            k_scr.attrs['frequency'] = np.float64(15.0)

        # Factories
        for idx, (fname, pname) in enumerate([('Steel_factory', 'Steel_Ball'), ('Ceramic_factory', 'Ceramic_Ball')], 1):
            gf = ggs.create_group(str(idx))
            gf.attrs['name'] = np.bytes_(fname.encode('ascii'))
            fac = gf.create_group(f'Factories/{fname}')
            fac.attrs['enabled'] = np.uint8(1)
            fac.attrs['unlimited'] = np.uint8(0)
            fac.attrs['is mass creation'] = np.uint8(0)
            fac.attrs['num to create'] = np.int32(500)
            fac.attrs['mass to create'] = np.float64(10.0)
            fac.attrs['creation rate'] = np.float64(1000.0)
            fac.attrs['creation time'] = np.float64(0.0)
            ft = fac.create_group('FactType')
            ft.create_dataset('type names', data=pname.encode('ascii'), dtype=dt_ascii)
            fs = fac.create_group(f'FactSize/{pname}')
            fs.attrs['type'] = np.bytes_(b'fixed')

        # PhysicsModels
        pm = cd.create_group('PhysicsModels')
        for cat in ('SurfaceSurface', 'SurfaceGeometry'):
            cg = pm.create_group(cat)
            cg.create_group('Hertz-Mindlin (no slip)')
            rf = cg.create_group('Standard Rolling Friction')
            rf.attrs['enable rolling friction'] = np.uint8(0)
            cg.create_dataset('order', data=np.array([b'Hertz-Mindlin (no slip)'], dtype=dt_ascii))

    dfg_text = "\r\n".join([
        "!format=KEYPATHVALUE",
        "!file-version=30.0.3",
        "PROCESSOR:GENERAL:AUTO TIMESTEP=BOOLEAN:FALSE",
        "PROCESSOR:GENERAL:FIXED TIMESTEP=DOUBLE:1.0e-06",
        "PROCESSOR:GENERAL:AUTO GRIDCELL=BOOLEAN:FALSE",
        "PROCESSOR:GENERAL:GRID CELL SIZE=DOUBLE:0.02",
        "PROCESSOR:GENERAL:TRACK COLLISIONS=BOOLEAN:FALSE",
        "PROCESSOR:GENERAL:NUM THREADS=INT:4",
        "PROCESSOR:WRITE:SELECTIVE SAVE=BOOLEAN:FALSE",
        "PROCESSOR:WRITE:INTERVAL=DOUBLE:0.05",
        "PROCESSOR:WRITE:TOTAL=DOUBLE:10",
        "",
    ])
    with open(dfg_path, 'w', encoding='utf-8', newline='') as fp:
        fp.write(dfg_text)

    return dem_path, h5_path, dfg_path


class TestEdemDeckEditor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_single_deck_edits_and_density_sync(self):
        dem, h5p, dfgp = create_synthetic_deck(self.tmp.name)
        esp.main([
            dem, '--no-backup', '--reset-zero',
            '--rpm', '60', '--clockwise',
            '--diameter-mm', '20', '--radius-material', 'Steel',
            '--material-density', 'Steel=8000',
            '--interaction-sf', 'Steel~Wall=0.62',
            '--interaction-rf', 'Wall~Steel=0.08',
            '--factory-mass', 'Ceramic_factory=3.5', '--fill-duration', '0.5',
            '--ball-count', 'Steel_factory=0',
            '--model-enable', 'SurfaceGeometry=Standard Rolling Friction',
            '--periodic', 'z=0',
            '--timestep', '2.5e-6', '--sim-time', '15',
        ])

        with h5py.File(dem, 'r') as fd:
            self.assertEqual(int(fd.attrs['num timesteps']), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'template_data', '1.h5')))

        with h5py.File(h5p, 'r') as f:
            cd = f['CreatorData/0']
            # 60 rpm clockwise around +Z -> -2*pi rad/s
            omega = float(cd['GeometryGroups/0/Kinematics/0'].attrs['initial angular velocity'])
            self.assertAlmostEqual(omega, -2.0 * math.pi, places=6)

            # Single-sphere radius (0.01 m) + updated density 8000 -> mass = 8000 * 4/3 * pi * 0.01^3
            p0 = cd['ParticleTypes/0']
            exp_vol = (4.0 / 3.0) * math.pi * (0.01 ** 3)
            exp_mass = 8000.0 * exp_vol
            self.assertAlmostEqual(float(p0.attrs['raw mass']), exp_mass, places=9)
            self.assertAlmostEqual(float(p0.attrs['raw inertia'][0]), 0.4 * exp_mass * (0.01 ** 2), places=12)

            # Factories: Steel disabled, Ceramic switched to mass creation
            f_steel = cd['GeometryGroups/1/Factories/Steel_factory']
            self.assertEqual(int(f_steel.attrs['enabled']), 0)
            f_cer = cd['GeometryGroups/2/Factories/Ceramic_factory']
            self.assertEqual(int(f_cer.attrs['enabled']), 1)
            self.assertEqual(int(f_cer.attrs['is mass creation']), 1)
            self.assertAlmostEqual(float(f_cer.attrs['mass to create']), 3.5)
            self.assertAlmostEqual(float(f_cer.attrs['creation rate']), 7.0)

            # Single-field interaction edits preserved restitution
            inter = cd['Interactions'][()]
            row = [r for r in inter if r['key'] == b'Steel~Wall'][0]
            self.assertAlmostEqual(float(row['restitution']), 0.5)
            self.assertAlmostEqual(float(row['staticFriction']), 0.62)
            self.assertAlmostEqual(float(row['rollingFriction']), 0.08)

            # Periodic Z turned off & rolling friction enabled
            self.assertEqual(int(cd['Domain'].attrs['domain zPeriodic']), 0)
            self.assertEqual(int(cd['PhysicsModels/SurfaceGeometry/Standard Rolling Friction'].attrs['enable rolling friction']), 1)

        dfg = esp.read_dfg_summary(dfgp)
        self.assertAlmostEqual(esp.parse_dfg_typed_value(dfg['PROCESSOR:GENERAL:FIXED TIMESTEP']), 2.5e-6)
        self.assertAlmostEqual(esp.parse_dfg_typed_value(dfg['PROCESSOR:WRITE:TOTAL']), 15.0)

    def test_multi_geometry_and_kinematic_attr(self):
        dem, h5p, _ = create_synthetic_deck(self.tmp.name, multi_geom=True)
        # Without --geometry, multiple Linear Rotation must fail loudly
        with self.assertRaises(SystemExit):
            esp.main([dem, '--no-backup', '--rpm', '30'])

        # With --geometry and --kinematic-attr
        esp.main([
            dem, '--no-backup',
            '--geometry', 'Impeller', '--rpm', '120',
            '--kinematic-attr', 'Screen:amplitude=0.005', 'Screen:frequency=20',
        ])
        with h5py.File(h5p, 'r') as f:
            cd = f['CreatorData/0']
            self.assertAlmostEqual(
                float(cd['GeometryGroups/3/Kinematics/0'].attrs['initial angular velocity']),
                120.0 * 2.0 * math.pi / 60.0,
                places=6,
            )
            self.assertAlmostEqual(float(cd['GeometryGroups/4/Kinematics/0'].attrs['amplitude']), 0.005)
            self.assertAlmostEqual(float(cd['GeometryGroups/4/Kinematics/0'].attrs['frequency']), 20.0)

    def test_multi_sphere_proportional_scaling(self):
        dem, h5p, _ = create_synthetic_deck(self.tmp.name, multi_sphere=True)
        esp.main([dem, '--no-backup', '--scale', '1.5', '--radius-material', 'Ceramic_Ball'])
        with h5py.File(h5p, 'r') as f:
            sp = f['CreatorData/0/ParticleTypes/1/spheres'][()]
            self.assertAlmostEqual(float(sp[0]['physicalRadius']), 0.012)
            self.assertAlmostEqual(float(sp[1]['physicalRadius']), 0.009)
            self.assertAlmostEqual(float(sp[0]['pos'][0]), -0.0075)
            self.assertAlmostEqual(float(sp[1]['pos'][0]), 0.0075)

    def test_export_template_csv(self):
        dem, _, _ = create_synthetic_deck(self.tmp.name)
        tpl = os.path.join(self.tmp.name, 'exported.csv')
        esp.main([dem, '--export-template-csv', tpl])
        with open(tpl, 'r', encoding='utf-8-sig') as fp:
            rows = list(csv.DictReader(fp))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['case_id'], 'BASE')
        self.assertIn('d_mm:Steel_Ball', rows[0])
        self.assertIn('count:Steel_factory', rows[0])
        self.assertIn('interaction:Steel~Wall', rows[0])

        # Round-trip: exported CSV must feed directly back into edem_batch.py without error
        rt_out = os.path.join(self.tmp.name, 'rt_batch')
        edem_batch.main(['--template', dem, '--matrix', tpl, '--out-dir', rt_out, '--shell', 'none'])
        self.assertTrue(os.path.exists(os.path.join(rt_out, 'BASE', 'BASE.dem')))

    def test_custom_cell_size_omits_grid_factor_in_queue(self):
        dem, _, _ = create_synthetic_deck(self.tmp.name)
        csv_path = os.path.join(self.tmp.name, 'custom_cell.csv')
        with open(csv_path, 'w', encoding='utf-8', newline='') as fp:
            w = csv.writer(fp)
            w.writerow(['case_id', 'cell_size_m', 'sf:Steel~Wall'])
            w.writerow(['G01', 0.045, 0.55])
            w.writerow(['G02', '', 0.65])
        out_dir = os.path.join(self.tmp.name, 'grid_out')
        edem_batch.main(['--template', dem, '--matrix', csv_path, '--out-dir', out_dir, '--shell', 'both'])

        with open(os.path.join(out_dir, 'run_batch_edem.ps1'), encoding='utf-8-sig') as fp:
            ps1 = fp.read()
        with open(os.path.join(out_dir, 'run_batch_edem.sh'), encoding='utf-8') as fp:
            sh = fp.read()
        g1_ps1 = [l for l in ps1.splitlines() if 'G01.dem' in l][0]
        g2_ps1 = [l for l in ps1.splitlines() if 'G02.dem' in l][0]
        g1_sh = [l for l in sh.splitlines() if 'G01.dem' in l][0]
        self.assertNotIn(' -g ', g1_ps1)
        self.assertNotIn(' -g ', g1_sh)
        self.assertIn(' -g 2.5', g2_ps1)

    def test_batch_csv_zero_diameter_and_periodic_preservation(self):
        """Regression test: d=0 on inactive species must NOT crash; periodic Z must NOT be silently cleared."""
        dem, _, _ = create_synthetic_deck(self.tmp.name)
        csv_path = os.path.join(self.tmp.name, 'cases.csv')
        with open(csv_path, 'w', encoding='utf-8', newline='') as fp:
            w = csv.writer(fp)
            w.writerow(['case_id', 'rpm', 'd_mm:Steel', 'd_mm:Ceramic', 'count:Steel_factory', 'count:Ceramic_factory', 'sf:Steel~Wall', 'timestep_us'])
            w.writerow(['C01', 30, 20, 0, 500, 0, 0.45, 7.5])
            w.writerow(['C02', 45, 0, 14, 0, 800, 0.60, 4.4])

        out_dir = os.path.join(self.tmp.name, 'batch_out')
        edem_batch.main([
            '--template', dem,
            '--matrix', csv_path,
            '--out-dir', out_dir,
            '--clockwise',
            '--shell', 'both',
        ])

        # Verify C01: Ceramic disabled, Steel radius=10mm, periodic Z preserved (=1!), .sh + .ps1 created
        c1_h5 = os.path.join(out_dir, 'C01', 'C01_data', '0.h5')
        with h5py.File(c1_h5, 'r') as f:
            cd = f['CreatorData/0']
            self.assertEqual(int(cd['GeometryGroups/1/Factories/Steel_factory'].attrs['enabled']), 1)
            self.assertEqual(int(cd['GeometryGroups/2/Factories/Ceramic_factory'].attrs['enabled']), 0)
            self.assertAlmostEqual(float(cd['ParticleTypes/0/spheres'][0]['physicalRadius']), 0.01)
            # Periodic Z was 1 in template and NOT touched -> must still be 1!
            self.assertEqual(int(cd['Domain'].attrs['domain zPeriodic']), 1)
            # Contact sf updated
            inter = cd['Interactions'][()]
            row = [r for r in inter if r['key'] == b'Steel~Wall'][0]
            self.assertAlmostEqual(float(row['staticFriction']), 0.45)

        # Verify C02: Steel disabled, Ceramic radius=7mm, cell size = 2.5 * 0.007 = 0.0175 m
        c2_dfg = esp.read_dfg_summary(os.path.join(out_dir, 'C02', 'C02.dfg'))
        self.assertAlmostEqual(esp.parse_dfg_typed_value(c2_dfg['PROCESSOR:GENERAL:GRID CELL SIZE']), 0.0175, places=6)
        self.assertAlmostEqual(esp.parse_dfg_typed_value(c2_dfg['PROCESSOR:GENERAL:FIXED TIMESTEP']), 4.4e-6, places=10)
        # Template sim_time=10 preserved
        self.assertAlmostEqual(esp.parse_dfg_typed_value(c2_dfg['PROCESSOR:WRITE:TOTAL']), 10.0)

        self.assertTrue(os.path.exists(os.path.join(out_dir, 'run_batch_edem.ps1')))
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'run_batch_edem.sh')))

    def test_batch_legacy_chinese_headers_compatibility(self):
        """Legacy Chinese ball-mill headers remain supported without breaking existing sheets."""
        dem, _, _ = create_synthetic_deck(self.tmp.name)
        csv_path = os.path.join(self.tmp.name, 'legacy.csv')
        with open(csv_path, 'w', encoding='utf-8-sig', newline='') as fp:
            w = csv.writer(fp)
            w.writerow(['工况编号', '目标转速 N (r/min)', '钢球直径 d_s (mm)', '瓷球直径 d_c (mm)', '钢球数量 (颗)', '瓷球数量 (颗)', '最小 15%T_R 步长 (μs)'])
            w.writerow(['L01', 35, 25, 18, 463, 1240, 0.75])

        out_dir = os.path.join(self.tmp.name, 'legacy_out')
        edem_batch.main([
            '--template', dem,
            '--xlsx', csv_path,
            '--out-dir', out_dir,
            '--periodic', 'z=0',
            '--fill-duration', '0.5',
            '--shell', 'ps1',
        ])
        with h5py.File(os.path.join(out_dir, 'L01', 'L01_data', '0.h5'), 'r') as f:
            cd = f['CreatorData/0']
            self.assertEqual(int(cd['GeometryGroups/1/Factories/Steel_factory'].attrs['num to create']), 463)
            self.assertEqual(int(cd['GeometryGroups/2/Factories/Ceramic_factory'].attrs['num to create']), 1240)
            self.assertEqual(int(cd['Domain'].attrs['domain zPeriodic']), 0)


if __name__ == '__main__':
    unittest.main()
