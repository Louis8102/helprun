*! helprun master validation entry point
*!
*! One invocation orchestrates the complete automated acceptance suite and
*! produces one consolidated result. Internal helpers exist, but the user is
*! never required to launch them separately.
*!
*! Run it with the working directory set to the project root:
*!
*!     cd <project root>
*!     "<Stata executable>" /e do test_helprun.do
*!
*! or point it at the root explicitly, from anywhere:
*!
*!     global HELPRUN_PROJECT "<project root>"
*!
*! Nothing here is hard-wired to one machine's drive layout, so the suite runs
*! from a clone in any directory (release assertion A8-16).

version 16.0
set more off
set linesize 120

* ------------------------------------------------------------------
* Locate the project root without hard-coding it.
* ------------------------------------------------------------------
if `"$HELPRUN_PROJECT"' != "" {
    local PROJECT `"$HELPRUN_PROJECT"'
}
else {
    local PROJECT `"`c(pwd)'"'
}

capture confirm file `"`PROJECT'/helprun.ado"'
if _rc {
    di as err "helprun: cannot find helprun.ado under:"
    di as err "    `PROJECT'"
    di as err "Run this file with the working directory set to the project"
    di as err "root, or set:  global HELPRUN_PROJECT " `"""' "<project root>" `"""'
    exit 601
}

local RUNDIR  "`PROJECT'\validation\gate3_run"

* Publish the root so every test file and every launched sub-process can
* find it. Stata's `python script` defines no __file__, so a test cannot
* derive the root from its own path; it reads this global, or the matching
* environment variable in a sub-process.
global HELPRUN_PROJECT `"`PROJECT'"'

python:
import os
from sfi import Macro
os.environ["HELPRUN_PROJECT"] = Macro.getGlobal("HELPRUN_PROJECT")
end

* ------------------------------------------------------------------
* Locate the Stata executable for the GUI sub-runs, without hard-coding it.
*
* The Viewer cases need a real GUI Stata, because `help` is ignored in batch
* mode. Stata reports its installation directory but not its own executable
* path, and the executable name carries the flavour, so the flavour Stata
* reports is used to pick it. HELPRUN_STATA_EXE overrides the search.
* ------------------------------------------------------------------
global HR_STATA_DIR    `"`c(sysdir_stata)'"'
global HR_STATA_FLAVOR `"`c(flavor)'"'

python:
import glob, os
from sfi import Macro

_exe = (Macro.getGlobal("HELPRUN_STATA_EXE") or "").strip()

if not _exe:
    _dir = Macro.getGlobal("HR_STATA_DIR") or ""
    _flavor = (Macro.getGlobal("HR_STATA_FLAVOR") or "").strip()
    _found = sorted(glob.glob(os.path.join(_dir, "Stata*.exe")))

    def _rank(p):
        n = os.path.basename(p).lower()
        return (
            0 if _flavor and _flavor.lower() in n else 1,
            0 if "-64" in n else 1,
            n,
        )

    _found.sort(key=_rank)
    _exe = _found[0] if _found else ""

Macro.setGlobal("HR_STATA_EXE", _exe)
end

if `"$HR_STATA_EXE"' == "" {
    di as err "helprun: could not locate a Stata executable under:"
    di as err "    $HR_STATA_DIR"
    di as err `"Set it explicitly:  global HELPRUN_STATA_EXE "<path to Stata.exe>""'
    exit 601
}

di as txt "Project root     : `PROJECT'"
di as txt "Stata executable : $HR_STATA_EXE"

di as txt "{hline 78}"
di as txt "helprun master validation suite"
di as txt "{hline 78}"
di as txt "Stata version    : " c(stata_version) "  edition " c(edition_real)
di as txt "OS               : " c(os) " " c(osdtl)
di as txt "Working directory: " c(pwd)
di as txt "{hline 78}"

capture mkdir "`RUNDIR'"

* ------------------------------------------------------------------
* Controlled ado path.
*
* PERSONAL precedes PLUS by default and contains an installed helprun copy
* (GATE 2 reference ledger, contamination hazard). The development tree is
* prepended so the code under test is unambiguously the one in this project,
* never a stale installed copy.
* ------------------------------------------------------------------
adopath ++ "`PROJECT'\fixtures"
adopath ++ "`PROJECT'\fixtures\adopath_sec"
adopath ++ "`PROJECT'\fixtures\adopath_pri"
adopath ++ "`PROJECT'"

quietly findfile helprun.ado
di as txt "helprun.ado under test : " as res "`r(fn)'"
quietly findfile _helprun.py
di as txt "_helprun.py under test : " as res "`r(fn)'"

* ------------------------------------------------------------------
* Viewer sub-run (U-C01..U-C03).
*
* GATE 2 R01 established that `help` is ignored in batch mode, so these cases
* are executed by a short GUI Stata sub-run that closes itself. Its results are
* read back and merged, so this remains a single entry point for the user.
* ------------------------------------------------------------------
capture erase "`RUNDIR'\results_viewer.txt"

di as txt ""
di as txt "Running Viewer sub-run in GUI Stata ..."

python:
import subprocess, os
from sfi import Macro

_exe = Macro.getGlobal("HR_STATA_EXE")
_root = Macro.getLocal("PROJECT")
_wd = os.path.join(_root, "validation")
_do = os.path.join(_wd, "gate3_run_viewer.do")

try:
    _p = subprocess.Popen([_exe, "do", _do, _root], cwd=_wd)
    _p.wait(timeout=240)
    Macro.setLocal("viewer_rc", str(_p.returncode))
except Exception as _exc:
    Macro.setLocal("viewer_rc", "launch-failed: " + str(_exc))
end

di as txt "Viewer sub-run finished (rc=`viewer_rc')"

* ------------------------------------------------------------------
* Static cases: source, parser, planning, guard, data resolution.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "GATE 3 static cases"
di as txt "{hline 78}"
python script "`PROJECT'\tests\gate3_static.py"

* ------------------------------------------------------------------
* Runtime cases: click/preparation, sandbox/executor, output/artifacts.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "GATE 3 runtime cases"
di as txt "{hline 78}"
python script "`PROJECT'\tests\gate3_runtime.py"

* ------------------------------------------------------------------
* GATE 4 Viewer sub-run (A42, A43), then the adversarial cases.
* ------------------------------------------------------------------
capture erase "`PROJECT'\validation\gate4_run\results_viewer4.txt"

di as txt ""
di as txt "Running GATE 4 Viewer sub-run in GUI Stata ..."

python:
import subprocess, os
from sfi import Macro

_exe = Macro.getGlobal("HR_STATA_EXE")
_root = Macro.getLocal("PROJECT")
_wd = os.path.join(_root, "validation")
_do = os.path.join(_wd, "gate4_run_viewer.do")

try:
    _p = subprocess.Popen([_exe, "do", _do, _root], cwd=_wd)
    _p.wait(timeout=300)
    Macro.setLocal("viewer4_rc", str(_p.returncode))
except Exception as _exc:
    Macro.setLocal("viewer4_rc", "launch-failed: " + str(_exc))
end

di as txt "GATE 4 Viewer sub-run finished (rc=`viewer4_rc')"

di as txt ""
di as txt "{hline 78}"
di as txt "GATE 4 adversarial cases"
di as txt "{hline 78}"
python script "`PROJECT'\tests\gate4_adversarial.py"

* ------------------------------------------------------------------
* GATE 6 named integration scenarios I01 .. I17.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "GATE 6 integration scenarios"
di as txt "{hline 78}"
python script "`PROJECT'\tests\gate6_scenarios.py"

* ------------------------------------------------------------------
* Historical bug regression HR-01 .. HR-20.
*
* One case per reproducible defect that GATE 3, GATE 4 and GATE 6 found and
* repaired. Every fixture is synthetic with a renamed topic, so no case here can
* pass because a real topic name appears in production.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "Historical bug regression"
di as txt "{hline 78}"
python script "`PROJECT'\tests\gate7_regression.py"

* ------------------------------------------------------------------
* Permanent production and help invariants.
*
* These are not a Gate of their own. They are the standing assertions that must
* hold on every run: the removed public example(#) UX must stay removed, no
* topic-specific branch may appear in production, and the published help must
* still describe the implementation it ships with.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "Permanent production and help invariants"
di as txt "{hline 78}"
python script "`PROJECT'\tests\gate7_invariants.py"

* ------------------------------------------------------------------
* Consolidated result.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "GATE 3 consolidated result"
di as txt "{hline 78}"

python script "`PROJECT'\tests\gate3_summary.py"

di as txt ""
di as txt "{hline 78}"
di as txt "GATE 4 consolidated result"
di as txt "{hline 78}"

python script "`PROJECT'\tests\gate4_summary.py"

* ------------------------------------------------------------------
* Deterministic real-help corpus.
*
* Last, because it is by far the longest part of the suite: it executes the
* frozen 70 BASE + 30 PLUS topic sample against real installed help. Set
* HELPRUN_SKIP_CORPUS to 1 to run only the fast sets while iterating; a release
* candidate must be validated with the corpus included.
* ------------------------------------------------------------------
if "$HELPRUN_SKIP_CORPUS" != "1" {
    di as txt ""
    di as txt "{hline 78}"
    di as txt "Deterministic real-help corpus"
    di as txt "{hline 78}"
    * The independent candidate scan comes first: gate6_corpus.py reconciles
    * production against the candidate list it writes, and reconciling against
    * a stale list would hide exactly the kind of regression GATE 9 caught.
    python script "`PROJECT'\tests\gate6_discovery.py"
    python script "`PROJECT'\tests\gate6_corpus.py"
    python script "`PROJECT'\tests\gate6_run_corpus.py"
}
else {
    di as txt ""
    di as txt "Corpus skipped (HELPRUN_SKIP_CORPUS=1). Not valid for a release candidate."
}

di as txt "{hline 78}"
di as txt "Master validation suite complete."
di as txt "{hline 78}"
