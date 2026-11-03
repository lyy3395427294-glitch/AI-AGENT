# -*- coding: utf-8 -*-
"""wormbox_clean.py 的纯逻辑测试（不需要 NX）。

只覆盖不依赖 NXOpen 的部分：参数校验、圆角边链分组、命令行/job 解析。
几何建模部分必须在 NX 里用 run_journal 验证，见 PR 说明。
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

os.environ["WORMBOX_NO_AUTORUN"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "wormbox_clean.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("wormbox_clean", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["wormbox_clean"] = module
    spec.loader.exec_module(module)
    return module


wb = _load_module()


class FakeVertex:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class FakeEdge:
    def __init__(self, *points):
        self._points = points

    def GetVertices(self):
        return [FakeVertex(*point) for point in self._points]


class ValidateParamsTest(unittest.TestCase):
    def test_default_params_are_valid(self):
        errors, warnings = wb.validate_params(dict(wb.DEFAULT_PARAMS))
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_cavity_must_be_smaller_than_body(self):
        params = dict(wb.DEFAULT_PARAMS, D_CAVITY=70.0)
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("D_CAVITY" in error for error in errors))

    def test_bore_must_be_smaller_than_cavity(self):
        params = dict(wb.DEFAULT_PARAMS, D_BORE=60.0)
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("D_BORE" in error for error in errors))

    def test_ring_hole_must_stay_in_ring(self):
        params = dict(wb.DEFAULT_PARAMS, R_RING_HOLE=10.0)
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("环上孔" in error for error in errors))

    def test_thread_format_and_diameter(self):
        params = dict(wb.DEFAULT_PARAMS, THREAD="M30 x 3.5")
        errors, warnings = wb.validate_params(params)
        self.assertEqual(errors, [])
        self.assertTrue(any("THREAD" in warning for warning in warnings))

        params["THREAD"] = "not-a-thread"
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("THREAD" in error for error in errors))

    def test_missing_and_unknown_parameters(self):
        params = dict(wb.DEFAULT_PARAMS)
        params.pop("D_BODY")
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("缺少参数" in error for error in errors))

        params = dict(wb.DEFAULT_PARAMS, TYPO_PARAM=1.0)
        _, warnings = wb.validate_params(params)
        self.assertTrue(any("TYPO_PARAM" in warning for warning in warnings))

    def test_non_numeric_ring_hole_count(self):
        params = dict(wb.DEFAULT_PARAMS, N_RING_HOLES="two")
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("N_RING_HOLES" in error for error in errors))

    def test_lugs_must_fit_in_body_length(self):
        params = dict(wb.DEFAULT_PARAMS, LUG_GAP=60.0)
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("耳片" in error for error in errors))

    def test_non_positive_fillet(self):
        params = dict(wb.DEFAULT_PARAMS, R_FILLET_RING=0.0)
        errors, _ = wb.validate_params(params)
        self.assertTrue(any("R_FILLET_RING" in error for error in errors))


class EdgeGroupingTest(unittest.TestCase):
    def test_edge_radii(self):
        edge = FakeEdge((10.0, 0.0, 0.0), (0.0, 0.0, -10.0))
        self.assertEqual(wb.edge_radii(edge), (10.0, 10.0))

    def test_shared_vertices_are_grouped_into_chains(self):
        first = FakeEdge((0, 0, 0), (1, 0, 0))
        second = FakeEdge((1, 0, 0), (2, 0, 0))
        third = FakeEdge((5, 0, 0), (6, 0, 0))
        groups = wb.group_tangent_edges([third, second, first])
        sizes = sorted(len(group) for group in groups)
        self.assertEqual(sizes, [1, 2])

    def test_order_is_deterministic(self):
        edges = [FakeEdge((2, 0, 0), (3, 0, 0)),
                 FakeEdge((0, 0, 0), (1, 0, 0)),
                 FakeEdge((1, 0, 0), (2, 0, 0))]
        first = [wb._edge_key(edge) for group in wb.group_tangent_edges(edges)
                 for edge in group]
        second = [wb._edge_key(edge) for group in wb.group_tangent_edges(
            list(reversed(edges))) for edge in group]
        self.assertEqual(first, second)

    def test_empty_input(self):
        self.assertEqual(wb.group_tangent_edges([]), [])


class CliTest(unittest.TestCase):
    def test_parse_check_flag(self):
        self.assertEqual(wb._parse_cli(["--check"]), (True, None, None))

    def test_parse_job_and_response(self):
        with tempfile.TemporaryDirectory() as folder:
            job = os.path.join(folder, "job.json")
            with open(job, "w", encoding="utf-8") as handle:
                json.dump({"params": {}}, handle)
            resp = os.path.join(folder, "res.json")
            self.assertEqual(wb._parse_cli([job, resp]), (False, job, resp))

    def test_missing_job_path_is_not_treated_as_job(self):
        # 不存在的 .json 不是 job；旧版在这种情况下也不会设置响应文件
        self.assertEqual(wb._parse_cli(["missing.json"]), (False, None, None))

    def test_placeholder_then_existing_response(self):
        # 旧用法 `-args x clean_res.json`：clean_res.json 已存在时仍是响应文件
        with tempfile.TemporaryDirectory() as folder:
            resp = os.path.join(folder, "clean_res.json")
            with open(resp, "w", encoding="utf-8") as handle:
                handle.write("{}")
            self.assertEqual(wb._parse_cli(["x", resp]), (False, None, resp))


class CheckModeTest(unittest.TestCase):
    def _run(self, *args):
        env = dict(os.environ)
        env.pop("WORMBOX_NO_AUTORUN", None)
        return subprocess.run(
            [sys.executable, SCRIPT, *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    def test_check_prints_valid_json(self):
        result = self._run("--check")
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["errors"], [])

    def test_bad_job_fails_with_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            job = os.path.join(folder, "job.json")
            with open(job, "w", encoding="utf-8") as handle:
                json.dump({"params": {"D_CAVITY": 100.0}}, handle)
            result = self._run("--check", job)
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertTrue(any("D_CAVITY" in error for error in report["errors"]))

    def test_unknown_job_key_is_warned(self):
        with tempfile.TemporaryDirectory() as folder:
            job = os.path.join(folder, "job.json")
            with open(job, "w", encoding="utf-8") as handle:
                json.dump({"out_step": "x.step"}, handle)
            result = self._run("--check", job)
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertTrue(any("out_step" in warning
                            for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
