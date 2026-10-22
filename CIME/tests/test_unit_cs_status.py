#!/usr/bin/env python3

import gzip
import io
import unittest
import shutil
import os
import tempfile
import re
from CIME.cs_status import cs_status
from CIME import test_status
from CIME.tests.custom_assertions_test_status import CustomAssertionsTestStatus


class TestCsStatus(CustomAssertionsTestStatus):

    # ------------------------------------------------------------------------
    # Test helper functions
    # ------------------------------------------------------------------------

    # An arbitrary phase we can use when we want to work with a non-core phase
    _NON_CORE_PHASE = test_status.MEMLEAK_PHASE

    # Another arbitrary phase if we need two different non-core phases
    _NON_CORE_PHASE2 = test_status.BASELINE_PHASE

    def setUp(self):
        self._testroot = tempfile.mkdtemp()
        self._output = io.StringIO()

    def tearDown(self):
        self._output.close()
        shutil.rmtree(self._testroot, ignore_errors=True)

    def create_test_dir(self, test_dir):
        """Creates the given test directory under testroot.

        Returns the full path to the created test directory.
        """
        fullpath = os.path.join(self._testroot, test_dir)
        os.makedirs(fullpath)
        return fullpath

    @staticmethod
    def create_test_status_core_passes(test_dir_path, test_name):
        """Creates a TestStatus file in the given path, with PASS status
        for all core phases"""
        with test_status.TestStatus(test_dir=test_dir_path, test_name=test_name) as ts:
            for phase in test_status.CORE_PHASES:
                ts.set_status(phase, test_status.TEST_PASS_STATUS)

    def set_last_core_phase_to_fail(self, test_dir_path, test_name):
        """Sets the last core phase to FAIL

        Returns the name of this phase"""
        fail_phase = test_status.CORE_PHASES[-1]
        self.set_phase_to_status(
            test_dir_path=test_dir_path,
            test_name=test_name,
            phase=fail_phase,
            status=test_status.TEST_FAIL_STATUS,
        )
        return fail_phase

    @staticmethod
    def set_phase_to_status(test_dir_path, test_name, phase, status):
        """Sets the given phase to the given status for this test"""
        with test_status.TestStatus(test_dir=test_dir_path, test_name=test_name) as ts:
            ts.set_status(phase, status)

    # ------------------------------------------------------------------------
    # Begin actual tests
    # ------------------------------------------------------------------------

    def test_force_rebuild(self):
        test_name = "my.test.name"
        test_dir = "my.test.name.testid"
        test_dir_path = self.create_test_dir(test_dir)
        self.create_test_status_core_passes(test_dir_path, test_name)
        cs_status(
            [os.path.join(test_dir_path, "TestStatus")],
            force_rebuild=True,
            out=self._output,
        )
        self.assert_status_of_phase(
            self._output.getvalue(),
            test_status.TEST_PEND_STATUS,
            test_status.SHAREDLIB_BUILD_PHASE,
            test_name,
        )

    def test_single_test(self):
        """cs_status for a single test should include some minimal expected output"""
        test_name = "my.test.name"
        test_dir = "my.test.name.testid"
        test_dir_path = self.create_test_dir(test_dir)
        self.create_test_status_core_passes(test_dir_path, test_name)
        cs_status([os.path.join(test_dir_path, "TestStatus")], out=self._output)
        self.assert_core_phases(self._output.getvalue(), test_name, fails=[])

    def test_two_tests(self):
        """cs_status for two tests (one with a FAIL) should include some minimal expected output"""
        test_name1 = "my.test.name1"
        test_name2 = "my.test.name2"
        test_dir1 = test_name1 + ".testid"
        test_dir2 = test_name2 + ".testid"
        test_dir_path1 = self.create_test_dir(test_dir1)
        test_dir_path2 = self.create_test_dir(test_dir2)
        self.create_test_status_core_passes(test_dir_path1, test_name1)
        self.create_test_status_core_passes(test_dir_path2, test_name2)
        test2_fail_phase = self.set_last_core_phase_to_fail(test_dir_path2, test_name2)
        cs_status(
            [
                os.path.join(test_dir_path1, "TestStatus"),
                os.path.join(test_dir_path2, "TestStatus"),
            ],
            out=self._output,
        )
        self.assert_core_phases(self._output.getvalue(), test_name1, fails=[])
        self.assert_core_phases(
            self._output.getvalue(), test_name2, fails=[test2_fail_phase]
        )

    def test_fails_only(self):
        """With fails_only flag, only fails and pends should appear in the output"""
        test_name = "my.test.name"
        test_dir = "my.test.name.testid"
        test_dir_path = self.create_test_dir(test_dir)
        self.create_test_status_core_passes(test_dir_path, test_name)
        fail_phase = self.set_last_core_phase_to_fail(test_dir_path, test_name)
        pend_phase = self._NON_CORE_PHASE
        self.set_phase_to_status(
            test_dir_path,
            test_name,
            phase=pend_phase,
            status=test_status.TEST_PEND_STATUS,
        )
        cs_status(
            [os.path.join(test_dir_path, "TestStatus")],
            fails_only=True,
            out=self._output,
        )
        self.assert_status_of_phase(
            output=self._output.getvalue(),
            status=test_status.TEST_FAIL_STATUS,
            phase=fail_phase,
            test_name=test_name,
        )
        self.assert_status_of_phase(
            output=self._output.getvalue(),
            status=test_status.TEST_PEND_STATUS,
            phase=pend_phase,
            test_name=test_name,
        )
        for phase in test_status.CORE_PHASES:
            if phase != fail_phase:
                self.assert_phase_absent(
                    output=self._output.getvalue(), phase=phase, test_name=test_name
                )
        self.assertNotRegex(self._output.getvalue(), r"Overall:")

    def test_count_fails(self):
        """Test the count of fails with three tests

        For first phase of interest: First test FAILs, second PASSes,
        third FAILs; count should be 2, and this phase should not appear
        individually for each test.

        For second phase of interest: First test PASSes, second PASSes,
        third FAILs; count should be 1, and this phase should not appear
        individually for each test.
        """
        # Note that this test does NOT cover:
        # - combining count_fails_phase_list with fails_only: currently,
        #   this wouldn't cover any additional code/logic
        # - ensuring that PENDs are also counted: currently, this
        #   wouldn't cover any additional code/logic
        phase_of_interest1 = self._NON_CORE_PHASE
        phase_of_interest2 = self._NON_CORE_PHASE2
        statuses1 = [
            test_status.TEST_FAIL_STATUS,
            test_status.TEST_PASS_STATUS,
            test_status.TEST_FAIL_STATUS,
        ]
        statuses2 = [
            test_status.TEST_PASS_STATUS,
            test_status.TEST_PASS_STATUS,
            test_status.TEST_FAIL_STATUS,
        ]
        test_paths = []
        test_names = []
        for testnum in range(3):
            test_name = "my.test.name" + str(testnum)
            test_names.append(test_name)
            test_dir = test_name + ".testid"
            test_dir_path = self.create_test_dir(test_dir)
            self.create_test_status_core_passes(test_dir_path, test_name)
            self.set_phase_to_status(
                test_dir_path,
                test_name,
                phase=phase_of_interest1,
                status=statuses1[testnum],
            )
            self.set_phase_to_status(
                test_dir_path,
                test_name,
                phase=phase_of_interest2,
                status=statuses2[testnum],
            )
            test_paths.append(os.path.join(test_dir_path, "TestStatus"))

        cs_status(
            test_paths,
            count_fails_phase_list=[phase_of_interest1, phase_of_interest2],
            out=self._output,
        )

        for testnum in range(3):
            self.assert_phase_absent(
                output=self._output.getvalue(),
                phase=phase_of_interest1,
                test_name=test_names[testnum],
            )
            self.assert_phase_absent(
                output=self._output.getvalue(),
                phase=phase_of_interest2,
                test_name=test_names[testnum],
            )
        count_regex1 = r"{} +non-passes: +2".format(re.escape(phase_of_interest1))
        self.assertRegex(self._output.getvalue(), count_regex1)
        count_regex2 = r"{} +non-passes: +1".format(re.escape(phase_of_interest2))
        self.assertRegex(self._output.getvalue(), count_regex2)

    def test_expected_fails(self):
        """With the expected_fails_file flag, expected failures should be flagged as such"""
        test_name1 = "my.test.name1"
        test_name2 = "my.test.name2"
        test_dir1 = test_name1 + ".testid"
        test_dir2 = test_name2 + ".testid"
        test_dir_path1 = self.create_test_dir(test_dir1)
        test_dir_path2 = self.create_test_dir(test_dir2)
        self.create_test_status_core_passes(test_dir_path1, test_name1)
        self.create_test_status_core_passes(test_dir_path2, test_name2)
        test1_fail_phase = self.set_last_core_phase_to_fail(test_dir_path1, test_name1)
        test2_fail_phase = self.set_last_core_phase_to_fail(test_dir_path2, test_name2)

        # One phase is labeled as an expected failure for test1, nothing for test2:
        expected_fails_contents = """<?xml version= "1.0"?>
<expectedFails version="1.1">
  <test name="{test_name1}">
    <phase name="{test1_fail_phase}">
      <status>{fail_status}</status>
    </phase>
  </test>
</expectedFails>
""".format(
            test_name1=test_name1,
            test1_fail_phase=test1_fail_phase,
            fail_status=test_status.TEST_FAIL_STATUS,
        )
        expected_fails_filepath = os.path.join(self._testroot, "ExpectedFails.xml")
        with open(expected_fails_filepath, "w") as expected_fails_file:
            expected_fails_file.write(expected_fails_contents)

        cs_status(
            [
                os.path.join(test_dir_path1, "TestStatus"),
                os.path.join(test_dir_path2, "TestStatus"),
            ],
            expected_fails_filepath=expected_fails_filepath,
            out=self._output,
        )

        # Both test1 and test2 should have a failure for one phase, but this should be
        # marked as expected only for test1.
        self.assert_core_phases(
            self._output.getvalue(), test_name1, fails=[test1_fail_phase]
        )
        self.assert_status_of_phase(
            self._output.getvalue(),
            test_status.TEST_FAIL_STATUS,
            test1_fail_phase,
            test_name1,
            xfail="expected",
        )
        self.assert_core_phases(
            self._output.getvalue(), test_name2, fails=[test2_fail_phase]
        )
        self.assert_status_of_phase(
            self._output.getvalue(),
            test_status.TEST_FAIL_STATUS,
            test2_fail_phase,
            test_name2,
            xfail="no",
        )
        # Make sure that no other phases are mistakenly labeled as expected failures:
        self.assert_num_expected_unexpected_fails(
            self._output.getvalue(), num_expected=1, num_unexpected=0
        )


# ----------------------------------------------------------------------------
# RUN PASS re-checked against the run logs
# ----------------------------------------------------------------------------

_TEST_NAME = "SMS.f19_g16.A.mach_comp"


def _make_test(tmp_path, run_status=test_status.TEST_PASS_STATUS):
    """A test directory whose TestStatus has every core phase PASS, with RUN
    set to run_status. Returns (test_dir, run_dir)."""
    test_dir = tmp_path / "{}.testid".format(_TEST_NAME)
    run_dir = tmp_path / "run"
    test_dir.mkdir()
    run_dir.mkdir()
    with test_status.TestStatus(test_dir=str(test_dir), test_name=_TEST_NAME) as ts:
        for phase in test_status.CORE_PHASES:
            ts.set_status(phase, test_status.TEST_PASS_STATUS)
        ts.set_status(test_status.MEMLEAK_PHASE, test_status.TEST_PASS_STATUS)
    if run_status != test_status.TEST_PASS_STATUS:
        with test_status.TestStatus(test_dir=str(test_dir)) as ts:
            ts.set_status(test_status.RUN_PHASE, run_status)
    return test_dir, run_dir


def _write_log(run_dir, name, text, mtime, compress=False):
    path = run_dir / name
    if compress:
        with gzip.open(str(path), "wt") as fd:
            fd.write(text)
    else:
        path.write_text(text)
    os.utime(str(path), (mtime, mtime))


def _status(test_dir, run_dir):
    out = io.StringIO()
    cs_status(
        [str(test_dir / "TestStatus")],
        out=out,
        run_dir_lookup=lambda _: None if run_dir is None else str(run_dir),
    )
    return out.getvalue()


def _run_line(output):
    """The RUN phase line of cs_status' detailed output."""
    return next(line for line in output.splitlines() if line.split()[2:3] == ["RUN"])


def test_run_pass_with_termination_message_stays_pass(tmp_path):
    test_dir, run_dir = _make_test(tmp_path)
    _write_log(run_dir, "atm.log.1.a", "...\n END OF MODEL RUN\n", 100)
    _write_log(run_dir, "cesm.log.1.a", "...\n", 100)
    output = _status(test_dir, run_dir)
    assert _run_line(output).split()[0] == test_status.TEST_PASS_STATUS
    assert "(Overall: PASS)" in output


def test_run_pass_without_termination_message_reported_as_fail(tmp_path):
    test_dir, run_dir = _make_test(tmp_path)
    _write_log(run_dir, "atm.log.1.a", "reading fields\n", 100, compress=True)
    _write_log(run_dir, "cesm.log.1.a", "MPI_Abort\n", 100, compress=True)
    before = (test_dir / "TestStatus").read_text()

    output = _status(test_dir, run_dir)

    line = _run_line(output)
    assert line.split()[0] == test_status.TEST_FAIL_STATUS
    assert "no log of run 1.a contains a termination message" in line
    assert "(Overall: FAIL)" in output
    # Later phases are still reported, and the file is unchanged.
    assert "{} {}".format(_TEST_NAME, test_status.MEMLEAK_PHASE) in output
    assert (test_dir / "TestStatus").read_text() == before


def test_only_the_newest_run_is_checked(tmp_path):
    test_dir, run_dir = _make_test(tmp_path)
    _write_log(run_dir, "atm.log.1.old", "crashed\n", 100)
    _write_log(run_dir, "drv.log.2.new", "SUCCESSFUL TERMINATION\n", 200)
    assert _run_line(_status(test_dir, run_dir)).split()[0] == "PASS"

    _write_log(run_dir, "atm.log.3.newest", "crashed\n", 300)
    assert _run_line(_status(test_dir, run_dir)).split()[0] == "FAIL"


def test_nothing_to_check_leaves_run_pass(tmp_path):
    test_dir, run_dir = _make_test(tmp_path)
    # No run directory known, then a run directory with no logs.
    assert _run_line(_status(test_dir, None)).split()[0] == "PASS"
    assert _run_line(_status(test_dir, run_dir)).split()[0] == "PASS"


def test_run_fail_is_not_rechecked(tmp_path):
    test_dir, run_dir = _make_test(tmp_path, run_status=test_status.TEST_FAIL_STATUS)
    _write_log(run_dir, "atm.log.1.a", "END OF MODEL RUN\n", 100)
    assert _run_line(_status(test_dir, run_dir)).split()[0] == "FAIL"


def test_override_status_keeps_later_phases_and_never_writes(tmp_path):
    test_dir, _ = _make_test(tmp_path)
    before = (test_dir / "TestStatus").read_text()
    with test_status.TestStatus(test_dir=str(test_dir)) as ts:
        ts.override_status(test_status.RUN_PHASE, test_status.TEST_FAIL_STATUS, "why")
        assert ts.get_status(test_status.MEMLEAK_PHASE) == test_status.TEST_PASS_STATUS
        assert ts.get_comment(test_status.RUN_PHASE) == "why"
    assert (test_dir / "TestStatus").read_text() == before


if __name__ == "__main__":
    unittest.main()
