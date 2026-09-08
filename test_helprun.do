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

* ------------------------------------------------------------------
* The validation trees must be present.
*
* This file is a MANDATORY member of the SSC package, but the tests/ and
* fixtures/ trees it orchestrates ship only in the paired GitHub release. An
* SSC user who runs it therefore has the entry point without its inputs, and
* the one thing that must never happen is that it looks like it passed. It
* refuses here, names what is missing, and says exactly where to get it.
* ------------------------------------------------------------------
capture confirm file `"`PROJECT'\tests\gate3_static.py"'
local hr_no_tests = _rc
capture confirm file `"`PROJECT'\fixtures\g3_out.sthlp"'
local hr_no_fixtures = _rc

if `hr_no_tests' | `hr_no_fixtures' {
    di as err "helprun: this is the master validation entry point, and its"
    di as err "validation trees are not present under:"
    di as err "    `PROJECT'"
    if `hr_no_tests'    di as err "    missing: tests\"
    if `hr_no_fixtures' di as err "    missing: fixtures\"
    di as err ""
    di as err "The SSC package ships the runtime plus this entry point. The"
    di as err "tests and fixtures ship in the paired GitHub release. Obtain"
    di as err "them and run this file from that tree."
    di as err ""
    di as err "NOTHING WAS VALIDATED."
    exit 601
}

local RUNDIR  "`PROJECT'\validation\gate3_run"

* Publish the root so every test file and every launched sub-process can
* find it. Stata's `python script` defines no __file__, so a test cannot
* derive the root from its own path; it reads this global, or the matching
* environment variable in a sub-process.
global HELPRUN_PROJECT `"`PROJECT'"'

* Environment row (HHARN-35). helprun depends on Stata's Python integration
* through the PERMANENT python_exec preference; an elevated Stata run once
* dropped it and nothing noticed until the next click failed. Checked BEFORE
* this file's own first python block, so a machine whose preference is missing
* gets a FAIL row (ENV-PYTHON) instead of a python error somewhere unrelated.
do "`PROJECT'\validation\env_check.do"
* External-UI containment (HHARN-41): this run is unattended; snapshot the browser
* tab strips ONCE, here, before any example executes. (It lived in env_check.do
* first, which interactive_contract.do also runs, so the rows file was reset
* mid-suite; a snapshot taken twice cannot attribute the pages between them.)
python: import os; os.environ["HELPRUN_UNATTENDED"] = "1"

* ------------------------------------------------------------------
* Clear any browser page a PREVIOUS run left open (HHARN-62), BEFORE the
* baseline snapshot below, so the baseline describes a clean desktop.
*
* The end-of-suite residue check can only run if the suite reaches its end, and
* when Stata's access violation ends a run part way through it never does: a
* stage killed mid-run cannot close its own pages, and a guard living inside
* the process that dies is no guard. Five consecutive runs on 2026-09-06 ended
* that way, the end-of-suite check never executed once, and four pages were
* left on the user's desktop with no containment record naming them.
*
* So the next run cleans up after the last one. This is recovery, not a
* verdict: it closes only what it can attribute to a document this project
* produced, leaves every other tab alone, and always succeeds. The
* end-of-suite check is the one that judges.
* ------------------------------------------------------------------
python script "`PROJECT'\tests\external_ui.py", args(--sweep)

* ------------------------------------------------------------------
* BROWSER-EXCLUSIVITY PRECONDITION (HHARN-68; user decision 2026-09-07).
*
* A release-candidate run requires that no browser window is open when it
* starts and that none is opened while it runs. This is a precondition of the
* RUN, not a relaxation of the attribution rule: page ownership is still
* established only by matching a page against a document helprun recorded,
* HHARN-64 still forbids treating unmatched newly appeared pages as clean, and
* nothing here infers ownership from a title.
*
* Why a precondition rather than smarter matching: attribution is negative, so
* a page a person opens by hand can never match a recorded document and is
* permanently unresolvable. Two suite runs on 2026-09-06 passed with
* appeared=12 attributable=12 and a third read UNRESOLVED on a Google tab and
* a Statalist thread, on identical code. Positive URL/path attribution is the
* real answer and is deferred to post-1.0 by the same decision.
*
* The check REFUSES and never closes: the sweep above has already closed what
* this project can prove it produced, so anything still open belongs to the
* user or cannot be proved ours, and in both cases the harness must stop and
* say so rather than decide what the user may have open.
* ------------------------------------------------------------------
capture noisily python script "`PROJECT'\tests\external_ui.py", args(--preflight)
if _rc {
    di as err ""
    di as err "helprun: master validation suite NOT STARTED."
    di as err "The browser-exclusivity precondition for a release-candidate run"
    di as err "is not satisfied. Close all browser windows and run this file"
    di as err "again; do not open a browser while it runs. Nothing was closed"
    di as err "for you, and no validation state was changed."
    exit 459
}

python script "`PROJECT'\tests\external_ui.py", args(--snapshot suite)

* ------------------------------------------------------------------
* This run's own completion record (HHARN-60).
*
* StataMp-64.exe takes an access violation at a fixed fault offset -- 24 times
* over the six days to 2026-09-06, on days before this session as well as
* during it -- and when it hits the suite's own process the run stops part way
* through, exits 0, and loses its buffered log. Every results file is then
* still present and green from an earlier run, so an abandoned run looked
* exactly like a finished one.
*
* The harness cannot stop Stata crashing. It can refuse to let an unfinished
* run pass for a finished one: this records that a run is in progress, and the
* matching call at the very end records that it completed. A run that dies
* leaves the first state behind and the terminal audit refuses it.
* ------------------------------------------------------------------
python script "`PROJECT'\tests\suite_record.py", args(--begin)


python:
import os
from sfi import Macro
os.environ["HELPRUN_PROJECT"] = Macro.getGlobal("HELPRUN_PROJECT")
end

* Unattended run: an example whose program asks the user for input runs in a
* visible worker (HPROD-42); nobody is here to answer, so the wait is bounded
* and such a run is reported as not answered, never SUCCESS. Production leaves
* the wait unbounded (the resumable human-input state).
python: import os; os.environ["HELPRUN_INPUT_WAIT_SECONDS"] = "45"


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
    # Specification 0.6.2: every automation-launched Stata is owned,
    # and its ownership record is what later authorises closing it
    # and proves it exited.
    import json as _json, time as _t
    _lc = os.path.join(_root, "validation", "gate7_run", "lifecycle")
    os.makedirs(_lc, exist_ok=True)
    with open(os.path.join(_lc, "suite_launches.jsonl"), "a",
              encoding="utf-8") as _fh:
        _fh.write(_json.dumps({"pid": _p.pid,
                               "purpose": os.path.basename(_do),
                               "start_time": _t.time()}) + chr(10))
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
    # Specification 0.6.2: every automation-launched Stata is owned,
    # and its ownership record is what later authorises closing it
    # and proves it exited.
    import json as _json, time as _t
    _lc = os.path.join(_root, "validation", "gate7_run", "lifecycle")
    os.makedirs(_lc, exist_ok=True)
    with open(os.path.join(_lc, "suite_launches.jsonl"), "a",
              encoding="utf-8") as _fh:
        _fh.write(_json.dumps({"pid": _p.pid,
                               "purpose": os.path.basename(_do),
                               "start_time": _t.time()}) + chr(10))
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
* Bounded help-document contract (HLP-01..HLP-17) and the harness self-audit.
*
* These were previously reachable only by launching their scripts separately,
* which the mandatory entry-point rule forbids: one invocation must orchestrate
* the complete automated suite. HLP-16 stays PENDING-GATE8 by design, because
* it compares the two built archive members.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "Public help contract"
di as txt "{hline 78}"
python script "`PROJECT'\tests\help_contract_hlp.py"

di as txt ""
di as txt "{hline 78}"
di as txt "Validation harness self-audit"
di as txt "{hline 78}"
python script "`PROJECT'\tests\gate7_harness_audit.py"

* ------------------------------------------------------------------
* Frozen-oracle and public-summary integrity.
*
* The oracle manifest is derived from the specification rather than maintained
* beside it, and the public validation index derives its counts from the
* recorded results. Both are verified here, never regenerated: a suite run may
* report drift, it may not quietly erase it.
* ------------------------------------------------------------------
* ------------------------------------------------------------------
* Harness sanity.
*
* Runs FIRST, and deliberately so. A patch once deleted three functions
* from the Viewer verifier; the file still parsed, so nothing caught it,
* and the failure surfaced as NameError / r(7103) in front of a person who
* had opened Stata to take a manual checkpoint. A broken harness must fail
* in seconds here, before anything else runs on top of it.
* ------------------------------------------------------------------
* ------------------------------------------------------------------
* Defect intake and post-verdict change control.
*
* Specification 0.6.1 required the defect lifecycle to run at discovery
* time, for a defect found by anyone. It is prose addressed to the agent,
* so nothing executed it, and it did not fire: a harness regression reached
* the user unrecorded. These two make the obligation executable -- an
* unreconciled incident, or a controlled artifact that moved after a verdict
* without its entry point being run, fails here.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "Defect intake and change control"
di as txt "{hline 78}"
python script "`PROJECT'\tests\incident_intake.py"
python script "`PROJECT'\tests\run_entrypoints.py"

* ------------------------------------------------------------------
* Interactive-input contract (HPROD-42, remainder of HPROD-34) and the
* nine-topic general-rule audit. Both run their own launchers so the roots
* come from this process's sysdir and the renamed fixture programs are on
* the adopath; neither launcher exits Stata.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "Interactive-input contract and nine-topic general-rule audit"
di as txt "{hline 78}"
do "`PROJECT'\validation\interactive_contract.do"
do "`PROJECT'\validation\nine_topic_audit.do"
do "`PROJECT'\validation\external_ui_contract.do"
* Final-artifact preservation and lifecycle contract (HPROD-49/50, HHARN-51):
* renamed external-runtime-like fixtures plus the real sparkta anchor.
do "`PROJECT'\validation\artifact_contract.do"
* Persistent example code artifact, standalone .do and run manifest
* (specification 12.4, scope decision SCOPE-002). It executes real examples, so
* it belongs here, ahead of the end-of-suite residue check.
do "`PROJECT'\validation\code_artifact.do"
* Segmentation, source faithfulness and public-surface provenance: three
* contracts written for the corrections of 2026-09-06, run here rather than
* separately so the master entry point is the only thing a user must launch.
python script "`PROJECT'\tests\segmentation.py"
python script "`PROJECT'\tests\source_faithfulness.py"
python script "`PROJECT'\tests\public_surface.py"
do "`PROJECT'\validation\manual_dependencies.do" check viewer


* ------------------------------------------------------------------
* User-facing log contract.
*
* helprun 1.0.0 shipped the raw child transcript inside the log a user
* opens. The clean-output assertions read parent Results and never opened
* the log, so the surface users actually read was never asserted on.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "User-facing log contract"
di as txt "{hline 78}"
python script "`PROJECT'\tests\user_log_contract.py"
python script "`PROJECT'\tests\planner_contract.py"
python script "`PROJECT'\tests\parser_contract.py"
python script "`PROJECT'\tests\input_staging_contract.py"
python script "`PROJECT'\tests\diagnostic_contract.py"

di as txt ""
di as txt "{hline 78}"
di as txt "Harness sanity"
di as txt "{hline 78}"
python script "`PROJECT'\tests\harness_sanity.py"

di as txt ""
di as txt "{hline 78}"
di as txt "Frozen oracle manifest"
di as txt "{hline 78}"
python script "`PROJECT'\tests\freeze_oracles.py"

di as txt ""
di as txt "{hline 78}"
di as txt "Public validation summary"
di as txt "{hline 78}"
python script "`PROJECT'\tests\validation_summary.py"

* ------------------------------------------------------------------
* Process lifecycle: verify every Stata this suite launched has exited,
* report any Stata it does NOT own without touching it, and record the
* evidence. Specification 0.6.2 L05/L08.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "Stata process lifecycle"
di as txt "{hline 78}"
python script "`PROJECT'\tests\suite_cleanup.py"
python script "`PROJECT'\tests\gate7_lifecycle.py"

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

* ------------------------------------------------------------------
* External-UI residue, at the TRUE end of the suite (EXTUI-08, HHARN-58/59).
*
* A stage that opens browser pages owns them, and its own containment report is
* not sufficient evidence: two stages reported closed=3 remaining=0 on
* 2026-09-06 with pages still open, because both numbers come from the same tab
* matching that had failed. This asserts from the desktop instead, and it must
* run LAST -- placed at the external-UI stage it would inspect a desktop that
* the artifact and code-artifact stages then dirty.
* ------------------------------------------------------------------
di as txt ""
di as txt "{hline 78}"
di as txt "External-UI residue (whole suite)"
di as txt "{hline 78}"
python script "`PROJECT'\tests\external_ui.py", args(--residue)

* The completion record, written only if the suite actually reached here.
python script "`PROJECT'\tests\suite_record.py", args(--end)

di as txt "{hline 78}"
di as txt "Master validation suite complete."
di as txt "{hline 78}"
