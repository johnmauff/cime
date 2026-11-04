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
    COMPARE_PHASE,
    TEST_PEND_STATUS,
    TEST_PASS_STATUS,
    TEST_FAIL_STATUS,
)
from CIME.case.case_run import TERMINATION_TEXT
import functools
import gzip
import os
import re
import subprocess
import sys
from collections import defaultdict

# A run log: <component>.log.<lid>, possibly gzipped.
_RUN_LOG_RE = re.compile(r"^\w+\.log\.(?P<lid>.+?)(\.gz)?$")

# Reported (never written) for a GPU test: its history files compared with
# those of its CPU counterpart, bit for bit.
CPU_GPU_COMPARE_PHASE = "{}_cpu_gpu".format(COMPARE_PHASE)

# A GPU test's testmod ends in "_gpu"; its CPU counterpart's testmod is the
# same name without it (testmods are joined with "--" in a test name).
_GPU_TESTMOD_RE = re.compile(r"_gpu(?=--|$)")

# A history file in a run directory: <case>.<component>.h<tape>....nc
_HIST_FILE_RE = re.compile(r"^\w+\.h\w*\..*\.nc$")

_CPRNC_IDENTICAL = "the two files seem to be IDENTICAL"
_CPRNC_FIELD_RE = re.compile(r"^\s*RMS\s+(\S+)")


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
    compare_cpu_gpu=True,
    cprnc=None,
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

    If compare_cpu_gpu is True, a GPU test (a testmod ending in ``_gpu``)
    gets a reported phase COMPARE_cpu_gpu: every history file of its newest
    run compared with cprnc against the same file of its CPU counterpart
    (the same test name without ``_gpu``), with zero tolerance. The
    counterpart is looked for among test_paths, preferring the same test
    id, then as a directory with the same test id next to the GPU test. The
    phase is PEND while either run is unfinished or no counterpart is found,
    and FAIL on any difference or missing file. cprnc(test_dir, file1,
    file2) returns cprnc's output for the GPU test in test_dir (default:
    run the case's CCSM_CPRNC).
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
    if cprnc is None:
        cprnc = _case_cprnc
    loaded = []
    for test_path in test_paths:
        test_dir = os.path.dirname(test_path)
        ts = TestStatus(test_dir=test_dir)

        if force_rebuild:
            with ts:
                ts.set_status(SHAREDLIB_BUILD_PHASE, TEST_PEND_STATUS)

        _recheck_run(ts, test_dir, run_dir_lookup)
        loaded.append((test_dir, ts))

    if compare_cpu_gpu:
        for test_dir, ts in loaded:
            _report_cpu_gpu_compare(test_dir, ts, loaded, run_dir_lookup, cprnc)

    for test_dir, ts in loaded:
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


def _recheck_run(ts, test_dir, run_dir_lookup):
    """Reports a RUN PASS whose newest run did not terminate as FAIL."""
    if ts.get_status(RUN_PHASE) == TEST_PASS_STATUS:
        problem = _unterminated_run(run_dir_lookup(test_dir))
        if problem is not None:
            ts.override_status(RUN_PHASE, TEST_FAIL_STATUS, problem)


def _cpu_test_name(test_name):
    """The CPU counterpart's test name of a GPU test, or None.

    Args:
        test_name (str): A test name, e.g. ``SMS.f19.A.mach_nvhpc.cam-foo_gpu``.

    Returns:
        str or None: The name without the ``_gpu`` testmod suffix, or None if
        test_name is not a GPU test.
    """
    cpu_name = _GPU_TESTMOD_RE.sub("", test_name, count=1)
    return None if cpu_name == test_name else cpu_name


def _find_cpu_counterpart(test_dir, cpu_name, loaded, run_dir_lookup):
    """Finds the CPU counterpart of the GPU test in test_dir.

    Args:
        test_dir (str): The GPU test's directory.
        cpu_name (str): The counterpart's test name.
        loaded (list): (test_dir, TestStatus) of every test being reported.
        run_dir_lookup (callable): Maps a test directory to its run directory.

    Returns:
        tuple: (test_dir, TestStatus) of the counterpart, or (None, reason).
    """
    test_id = os.path.basename(test_dir).split(".")[-1]
    candidates = [(d, ts) for d, ts in loaded if ts.get_name() == cpu_name]
    same_id = [
        (d, ts) for d, ts in candidates if os.path.basename(d).split(".")[-1] == test_id
    ]
    if len(same_id) == 1:
        return same_id[0]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return None, "several CPU counterparts: {}".format(
            ", ".join(sorted(os.path.basename(d) for d, _ in candidates))
        )
    sibling = os.path.join(
        os.path.dirname(test_dir), "{}.{}".format(cpu_name, test_id)
    )
    if os.path.isfile(os.path.join(sibling, "TestStatus")):
        ts = TestStatus(test_dir=sibling)
        _recheck_run(ts, sibling, run_dir_lookup)
        return sibling, ts
    return (
        None,
        "no CPU counterpart {} found (report it too: -t <its test id>)".format(
            cpu_name
        ),
    )


def _report_cpu_gpu_compare(test_dir, ts, loaded, run_dir_lookup, cprnc):
    """Adds the CPU/GPU comparison phase to the TestStatus of a GPU test."""
    cpu_name = _cpu_test_name(ts.get_name())
    if cpu_name is None:
        return
    cpu_dir, cpu_ts = _find_cpu_counterpart(test_dir, cpu_name, loaded, run_dir_lookup)
    if cpu_dir is None:
        ts.report_status(CPU_GPU_COMPARE_PHASE, TEST_PEND_STATUS, cpu_ts)
        return
    against = "vs {}".format(os.path.basename(cpu_dir))
    if (
        ts.get_status(RUN_PHASE) != TEST_PASS_STATUS
        or cpu_ts.get_status(RUN_PHASE) != TEST_PASS_STATUS
    ):
        ts.report_status(
            CPU_GPU_COMPARE_PHASE,
            TEST_PEND_STATUS,
            "{}: waiting for both runs to PASS".format(against),
        )
        return
    status, detail = _compare_history(
        run_dir_lookup(test_dir),
        os.path.basename(test_dir),
        run_dir_lookup(cpu_dir),
        os.path.basename(cpu_dir),
        lambda gpu_file, cpu_file: cprnc(test_dir, cpu_file, gpu_file),
    )
    ts.report_status(CPU_GPU_COMPARE_PHASE, status, "{}: {}".format(against, detail))


def _history_files(run_dir, case):
    """Maps each history file of case in run_dir to its name after the case."""
    prefix = case + "."
    if run_dir is None or not os.path.isdir(run_dir):
        return {}
    return {
        name[len(prefix) :]: os.path.join(run_dir, name)
        for name in os.listdir(run_dir)
        if name.startswith(prefix) and _HIST_FILE_RE.match(name[len(prefix) :])
    }


def _compare_history(gpu_run_dir, gpu_case, cpu_run_dir, cpu_case, compare):
    """Compares every history file of the GPU run with the CPU run's.

    Args:
        gpu_run_dir (str): The GPU test's run directory.
        gpu_case (str): The GPU case name (history file prefix).
        cpu_run_dir (str): The CPU test's run directory.
        cpu_case (str): The CPU case name.
        compare (callable): compare(gpu_file, cpu_file) returns cprnc output.

    Returns:
        tuple: (status, detail). PASS only if both runs wrote the same files
        and cprnc finds every pair IDENTICAL (zero tolerance).
    """
    gpu_files = _history_files(gpu_run_dir, gpu_case)
    cpu_files = _history_files(cpu_run_dir, cpu_case)
    if not gpu_files:
        return TEST_FAIL_STATUS, "no history files in the GPU run"
    only_gpu = sorted(set(gpu_files) - set(cpu_files))
    only_cpu = sorted(set(cpu_files) - set(gpu_files))
    if only_gpu or only_cpu:
        return (
            TEST_FAIL_STATUS,
            "history files differ: only GPU {}, only CPU {}".format(
                only_gpu or "-", only_cpu or "-"
            ),
        )
    different = []
    for key in sorted(gpu_files):
        output = compare(gpu_files[key], cpu_files[key])
        if _CPRNC_IDENTICAL not in output:
            fields = []
            for line in output.splitlines():
                match = _CPRNC_FIELD_RE.match(line)
                if match and match.group(1) not in fields:
                    fields.append(match.group(1))
            if not fields:
                lines = [line.strip() for line in output.splitlines() if line.strip()]
                fields = [lines[-1] if lines else "no cprnc output"]
            different.append("{} ({})".format(key, ", ".join(fields)))
    if different:
        return TEST_FAIL_STATUS, "DIFFERENT {} of {} files: {}".format(
            len(different), len(gpu_files), "; ".join(different)
        )
    return TEST_PASS_STATUS, "{} history files IDENTICAL".format(len(gpu_files))


@functools.lru_cache(maxsize=None)
def _case_cprnc_exe(test_dir):
    """The case's CCSM_CPRNC."""
    from CIME.case import Case

    with Case(test_dir, read_only=True) as case:
        return case.get_value("CCSM_CPRNC")


def _case_cprnc(test_dir, file1, file2):
    """Runs the case's cprnc (CCSM_CPRNC) on two files; returns its output."""
    try:
        result = subprocess.run(
            [_case_cprnc_exe(test_dir), "-m", file1, file2],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            check=False,
        )
    except (OSError, TypeError) as err:
        return "cprnc could not run: {}".format(err)
    return result.stdout


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
