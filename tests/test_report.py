import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server.stages
from server import pipeline, report
from server.pipeline import Job

FOOTPRINT = """\
======================================================================
brush [93469]: 64-bit    Footprint: 24.04 GB (16384 bytes per page)
======================================================================
python3.12 [47001]: 64-bit    Footprint: 812 MB (16384 bytes per page)
"""

VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               11162.
Pages active:                            177491.
Pages wired down:                        650272.
Pages purgeable:                           4579.
Anonymous pages:                         300000.
Pages occupied by compressor:            100000.
"""


class ParserTests(unittest.TestCase):
    def test_process_tree(self):
        procs = report.parse_ps("  1     0  0.0  100\n 10     1 50.0 2048\n 11    10 25.5 1024\n 12     2  1.0 9\n")
        self.assertEqual(sorted(report.descendants(procs, 10)), [10, 11])
        self.assertEqual(procs[11], (10, 25.5, 1024))

    def test_footprint_counts_gpu_memory_per_process(self):
        fp = report.parse_footprint(FOOTPRINT)
        self.assertAlmostEqual(fp[93469], 24.04 * 1024)
        self.assertEqual(fp[47001], 812)

    def test_vm_stat(self):
        vm = report.parse_vm_stat(VM_STAT)
        self.assertAlmostEqual(vm["wired_mb"], 650272 * 16384 / 2**20)
        self.assertAlmostEqual(vm["app_mb"], (300000 - 4579) * 16384 / 2**20)
        self.assertAlmostEqual(vm["compressed_mb"], 100000 * 16384 / 2**20)

    def test_swap_and_power(self):
        self.assertEqual(report.parse_swap("total = 14336.00M  used = 13375.69M  free = 960.31M  (encrypted)"), 13375.69)
        self.assertIsNone(report.parse_swap(""))
        self.assertEqual(report.parse_power("Now drawing from 'Battery Power'\n -InternalBattery-0 41%; discharging"),
                         {"source": "Battery", "percent": 41})


class SummaryTests(unittest.TestCase):
    samples = [
        {"t": 0, "cpu_pct": 10, "footprint_mb": 1000, "swap_mb": 500},
        {"t": 5, "cpu_pct": 30, "footprint_mb": 3000, "swap_mb": 900},
        {"t": 400, "cpu_pct": 20, "footprint_mb": 2000, "swap_mb": 700, "stage": "train"},
    ]

    def test_stage_summary(self):
        s = report.summarize(self.samples, 0, 10)
        self.assertEqual((s["samples"], s["peak_footprint_mb"], s["mean_cpu_pct"], s["swap_growth_mb"]), (2, 3000, 20.0, 400))
        self.assertEqual(report.summarize(self.samples, 1000, 2000), {"samples": 0})

    def test_a_long_silence_reads_as_sleep(self):
        gaps = report.sleep_gaps(self.samples)
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0]["minutes"], 6.6)


class PipelineReportTests(unittest.TestCase):
    def run_job(self, train):
        def stage(name):
            return SimpleNamespace(run=lambda job, work, preset: report.note(job, name, ran=True))
        fakes = {"frames": stage("frames"), "poses_colmap": stage("poses"), "poses_da3": stage("poses"),
                 "train_brush": SimpleNamespace(run=train), "export": stage("export"), "mesh": stage("mesh")}
        modules = {f"server.stages.{n}": m for n, m in fakes.items()}
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, modules), \
             patch.multiple(server.stages, create=True, **fakes):
            job = Job("r", "high", "colmap", "splat")
            job.work = Path(temp)
            pipeline._run_sync(job)
            state = job.snapshot()[0]
            text = (job.work / "exports" / report.REPORT_NAME).read_text()
            return state, text

    def test_a_finished_job_lists_its_report(self):
        state, text = self.run_job(lambda job, work, preset: report.note(job, "train", images=3))
        self.assertEqual(state["stage"], "done")
        self.assertIn(report.REPORT_NAME, [a["name"] for a in state["artifacts"]])
        self.assertIn("**Outcome:** done", text)
        for stage in ("frames", "poses", "train", "export"):
            self.assertIn(f"| {stage} |", text)
        self.assertIn('"images": 3', text)  # notes reach the raw data

    def test_a_failed_job_still_writes_its_report(self):
        def boom(job, work, preset):
            raise RuntimeError("brush training failed")
        state, text = self.run_job(boom)
        self.assertEqual((state["stage"], state["failed_stage"]), ("error", "train"))
        self.assertIn("failed at train: brush training failed", text)
        self.assertNotIn("| export |", text)


if __name__ == "__main__":
    unittest.main()
