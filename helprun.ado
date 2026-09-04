*! version 1.0.0
*! helprun -- run a Stata help example by clicking it in the Viewer
*!
*! Usage:
*!     help somecommand
*!     helprun
*! then click "Run this example" on the example you want.
*!
*! Implementation note: every Python call below is a single-line `python:`
*! statement. A `python:` ... `end` block cannot be used inside
*! `program define ... end`, because Stata would treat the block's `end` as the
*! end of the program definition and the ado file would fail to load.

program define helprun, rclass
    version 16.0

    * hrclick() is internal and undocumented. It carries a source-bound
    * identity token that helprun itself generates inside the temporary
    * clickable Viewer. It is not a way for a user to choose an example.
    syntax [, HRCLICK(string)]

    quietly findfile _helprun.py
    local helper `"`r(fn)'"'

    local base     : sysdir BASE
    local site     : sysdir SITE
    local plus     : sysdir PLUS
    local personal : sysdir PERSONAL

    if `"`hrclick'"' == "" {
        helprun_prepare, helper(`"`helper'"') ///
            base(`"`base'"') site(`"`site'"') ///
            plus(`"`plus'"') personal(`"`personal'"')

        return local topic    `"`s(topic)'"'
        return local source   `"`s(source)'"'
        return local viewfile `"`s(viewfile)'"'
        return scalar n_examples = real(`"`s(n_examples)'"')
        exit
    }

    * The parent working directory is captured at click time and becomes the
    * frozen destination for this run's log and artifacts. A later cd inside
    * the hidden child cannot redirect them.
    helprun_click, helper(`"`helper'"') token(`"`hrclick'"') ///
        pwd(`"`c(pwd)'"') ///
        base(`"`base'"') site(`"`site'"') ///
        plus(`"`plus'"') personal(`"`personal'"')

    return local status  `"`s(status)'"'
    return local reason  `"`s(reason)'"'
    return local logfile `"`s(logfile)'"'
end


* ------------------------------------------------------------------
* Preparation only.
*
* Identifies the active help Viewer for THIS Stata process, reads and parses
* the help source graph, and writes a temporary clickable view. It executes no
* example, launches no child Stata, downloads nothing, and creates no learning
* artifacts. Only clicking a specific example does any of that.
* ------------------------------------------------------------------
program define helprun_prepare, sclass
    syntax , helper(string) base(string) site(string) ///
        plus(string) personal(string)

    sreturn clear

    local hr_ok ""
    local hr_topic ""
    local hr_source ""
    local hr_viewfile ""
    local hr_n_examples ""
    local hr_reason ""
    local hr_error ""

    python: import importlib.util as _I; from sfi import Macro as _M; _s=_I.spec_from_file_location("_helprun_runtime",_M.getLocal("helper")); _m=_I.module_from_spec(_s); _s.loader.exec_module(_m); _z=_m.ado_prepare([_M.getLocal("base"),_M.getLocal("site"),_M.getLocal("plus"),_M.getLocal("personal")]); exec("for _k,_v in _z.items(): _M.setLocal('hr_'+_k,_v)")

    if "`hr_ok'" != "1" {
        di as err `"`hr_error'"'
        exit 498
    }

    view file `"`hr_viewfile'"'

    di as txt "helprun: " as res "`hr_n_examples'" ///
        as txt " runnable example(s) in " as res "`hr_topic'"
    di as txt "helprun: click {bf:Run this example} on the one you want."

    sreturn local topic      `"`hr_topic'"'
    sreturn local source     `"`hr_source'"'
    sreturn local viewfile   `"`hr_viewfile'"'
    sreturn local n_examples `"`hr_n_examples'"'
end


* ------------------------------------------------------------------
* Click: execute exactly the example the user clicked.
* ------------------------------------------------------------------
program define helprun_click, sclass
    syntax , helper(string) token(string) pwd(string) ///
        base(string) site(string) plus(string) personal(string)

    sreturn clear

    local hr_status ""
    local hr_reason ""
    local hr_failure_class ""
    local hr_message ""
    local hr_logfile ""
    local hr_plan ""
    local hr_artifacts ""
    local hr_resultsfile ""
    local hr_location ""
    local hr_causal ""
    local hr_rcode ""

    python: import importlib.util as _I; from sfi import Macro as _M; _s=_I.spec_from_file_location("_helprun_runtime",_M.getLocal("helper")); _m=_I.module_from_spec(_s); _s.loader.exec_module(_m); _z=_m.ado_click(_M.getLocal("token"),_M.getLocal("pwd"),[_M.getLocal("base"),_M.getLocal("site"),_M.getLocal("plus"),_M.getLocal("personal")]); exec("for _k,_v in _z.items(): _M.setLocal('hr_'+_k,_v)")

    * ------------------------------------------------------------------
    * Output bridge, specification section 12.1A.
    *
    * Results shows the authored commands and their ordinary Stata results
    * ONCE, then one concise location line. It must look like normal
    * interactive Stata: no run-log header, no internal child framing, no
    * duplicate COMMANDS EXECUTED / CHILD OUTPUT streams, no instrumentation,
    * and no artifact manifest. Those all remain in the persistent log.
    * ------------------------------------------------------------------
    if `"`hr_resultsfile'"' != "" {
        capture confirm file `"`hr_resultsfile'"'
        if !_rc {
            type `"`hr_resultsfile'"'
            capture erase `"`hr_resultsfile'"'
        }
    }

    if "`hr_status'" != "SUCCESS" {
        * The meaningful causal Stata message, not `end of do-file`.
        if `"`hr_causal'"' != "" {
            di as err `"`hr_causal'"'
            if `"`hr_rcode'"' != "" {
                di as err "r(`hr_rcode');"
            }
        }
        else if `"`hr_message'"' != "" {
            di as err `"`hr_message'"'
        }
    }

    * Exactly one concise absolute location line. No `ARTIFACTS: none`, no
    * standalone `none`, and no file-by-file manifest.
    if `"`hr_location'"' != "" {
        di as txt `"`hr_location'"'
    }

    sreturn local status  `"`hr_status'"'
    sreturn local reason  `"`hr_reason'"'
    sreturn local logfile `"`hr_logfile'"'
end
