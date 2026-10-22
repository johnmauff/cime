"""
Implementation of the cs.status script, which prints the status of all
of the tests in one or more test suites
"""

from __future__ import print_function
from CIME.XML.standard_module_setup import *
from CIME.XML.expected_fails_file import ExpectedFailsFile
from CIME.test_status import (
    TestStatus,
    SHAREDLIB_BUILD_PHASE,
    RUN_PHASE,
    TEST_PEND_STATUS,
    TEST_PASS_STATUS,
    TEST_FAIL_STATUS,
)
from CIME.case.case_run import TERMINATION_TEXT
import gzip
import os
import re
import sys
from collections import defaultdict

# A run log: <component>.log.<lid>, possibly gzipped.
_RUN_LOG_RE = re.compile(r"^\w+\.log\.(?P<lid>.+?)(\.gz)?$")


def cs_status(
    test_paths,
    summary=False,
    fails_only=False,
    count_fails_phase_list=None,
    check_throughput=False,
    check_memory=False,
    expected_fails_filepath=None,
    force_rebuild=False,
    out=sys.stdout,
    run_dir_lookup=None,
):
    """Print the test statuses of all tests in test_paths. The default
    is to print to stdout, but this can be overridden with the 'out'
    argument.

    If summary is True, then only the overall status of each test is printed

    If fails_only is True, then only test failures are printed (this
    includes PENDs as well as FAILs).

    If count_fails_phase_list is provided, it should be a list of phases
    (from the phases given by test_status.ALL_PHASES). For each phase in
    this list: do not give line-by-line output; instead, just report the
    total number of tests that have not PASSed this phase (this includes
    PENDs and FAILs). (This is typically used with the fails_only
    option, but it can also be used without that option.)

    If expected_fails_filepath is provided, it should be a string giving
    the full path to a file listing expected failures for this test
    suite. Expected failures are then labeled as such in the output.

    A RUN phase recorded as PASS is re-checked against the run logs: if no
    log of the newest run contains a termination message, RUN is reported
    as FAIL (the TestStatus file is not changed). The recorded PASS can be
    wrong when the MPI launcher hides an abort's exit status and the
    post-run check only finds a non-empty component log.
    run_dir_lookup maps a test directory to its run directory (default:
    the case's RUNDIR); None from it skips the check.
    """
    expect(not (summary and fails_only), "Cannot have both summary and fails_only")
    expect(
        not (summary and count_fails_phase_list),
        "Cannot have both summary and count_fails_phase_list",
    )
    if count_fails_phase_list is None:
        count_fails_phase_list = []
    non_pass_counts = dict.fromkeys(count_fails_phase_list, 0)
    xfails = _get_xfails(expected_fails_filepath)
    test_id_output = defaultdict(str)
    test_id_counts = defaultdict(int)
    if run_dir_lookup is None:
        run_dir_lookup = _case_run_dir
    for test_path in test_paths:
        test_dir = os.path.dirname(test_path)
        ts = TestStatus(test_dir=test_dir)

        if force_rebuild:
            with ts:
                ts.set_status(SHAREDLIB_BUILD_PHASE, TEST_PEND_STATUS)

        if ts.get_status(RUN_PHASE) == TEST_PASS_STATUS:
            problem = _unterminated_run(run_dir_lookup(test_dir))
            if problem is not None:
                ts.override_status(RUN_PHASE, TEST_FAIL_STATUS, problem)

        test_id = os.path.basename(test_dir).split(".")[-1]
        if summary:
            output = _overall_output(
                ts, "  {status} {test_name}\n", check_throughput, check_memory
            )
        else:
            if fails_only:
                output = ""
            else:
                output = _overall_output(
                    ts,
                    "  {test_name} (Overall: {status}) details:\n",
                    check_throughput,
                    check_memory,
                )
            output += ts.phase_statuses_dump(
                prefix="    ",
                skip_passes=fails_only,
                skip_phase_list=count_fails_phase_list,
                xfails=xfails.get(ts.get_name()),
            )
            if count_fails_phase_list:
                ts.increment_non_pass_counts(non_pass_counts)

        test_id_output[test_id] += output
        test_id_counts[test_id] += 1

    for test_id in sorted(test_id_output):
        count = test_id_counts[test_id]
        print(
            "{}: {} test{}".format(test_id, count, "s" if count > 1 else ""), file=out
        )
        print(test_id_output[test_id], file=out)
        print(" ", file=out)

    if count_fails_phase_list:
        print(72 * "=", file=out)
        print("Non-PASS results for select phases:", file=out)
        for phase in count_fails_phase_list:
            print("{} non-passes: {}".format(phase, non_pass_counts[phase]), file=out)


def _case_run_dir(test_dir):
    """Returns the RUNDIR of the case in test_dir, or None if it can't be read."""
    if not os.path.isfile(os.path.join(test_dir, "env_run.xml")):
        return None
    try:
        from CIME.case import Case

        with Case(test_dir, read_only=True) as case:
            return case.get_value("RUNDIR")
    except Exception:  # pylint: disable=broad-except
        return None


def _unterminated_run(run_dir):
    """Checks that the newest run in run_dir terminated.

    Args:
        run_dir (str or None): The run directory.

    Returns:
        str or None: Why the run is not known to have terminated, or None if
        a log of the newest run contains a termination message, or if there
        is nothing to check (no run directory or no run logs).
    """
    if run_dir is None or not os.path.isdir(run_dir):
        return None
    logs = {}
    for name in os.listdir(run_dir):
        match = _RUN_LOG_RE.match(name)
        if match:
            logs[os.path.join(run_dir, name)] = match.group("lid")
    if not logs:
        return None
    lid = logs[max(logs, key=os.path.getmtime)]
    for path, log_lid in logs.items():
        if log_lid != lid:
            continue
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as fd:
            text = fd.read()
        if any(marker in text for marker in TERMINATION_TEXT):
            return None
    return "TestStatus says PASS, but no log of run {} contains a termination message".format(
        lid
    )


def _get_xfails(expected_fails_filepath):
    """Returns a dictionary of ExpectedFails objects, where the keys are test names

    expected_fails_filepath should be either a string giving the path to
    the file containing expected failures, or None. If None, then this
    returns an empty dictionary (as if expected_fails_filepath were
    pointing to a file with no expected failures listed).
    """
    if expected_fails_filepath is not None:
        expected_fails_file = ExpectedFailsFile(expected_fails_filepath)
        xfails = expected_fails_file.get_expected_fails()
    else:
        xfails = {}
    return xfails


def _overall_output(ts, format_str, check_throughput, check_memory):
    """Returns a string giving the overall test status

    Args:
    ts: TestStatus object
    format_str (string): string giving the format of the output; must
        contain place-holders for status and test_name
    """
    test_name = ts.get_name()
    status = ts.get_overall_test_status(
        check_throughput=check_throughput,
        check_memory=check_memory,
    )[0]
    return format_str.format(status=status, test_name=test_name)
