*! version 2.0.0
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

    python: import importlib.util as _I; from sfi import Macro as _M; _s=_I.spec_from_file_location("_helprun_runtime",_M.getLocal("helper")); _m=_I.module_from_spec(_s); _s.loader.exec_module(_m); _z=_m.ado_prepare([_M.getLocal("base"),_M.getLocal("site"),_M.getLocal("plus"),_M.getLocal("personal")]); [_M.setLocal("hr_"+_k,_v) for _k,_v in _z.items()]

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

    python: import importlib.util as _I; from sfi import Macro as _M; _s=_I.spec_from_file_location("_helprun_runtime",_M.getLocal("helper")); _m=_I.module_from_spec(_s); _s.loader.exec_module(_m); _z=_m.ado_click(_M.getLocal("token"),_M.getLocal("pwd"),[_M.getLocal("base"),_M.getLocal("site"),_M.getLocal("plus"),_M.getLocal("personal")]); [_M.setLocal("hr_"+_k,_v) for _k,_v in _z.items()]

    * Output bridge: the user sees what the clicked example produced without
    * having to open the log file.
    if `"`hr_logfile'"' != "" {
        capture confirm file `"`hr_logfile'"'
        if !_rc {
            di as txt ""
            di as txt "{hline 60}"
            type `"`hr_logfile'"'
            di as txt "{hline 60}"
        }
    }

    if "`hr_status'" == "SUCCESS" {
        di as txt "helprun: " as res "SUCCESS" ///
            as txt "  examples run: [" as res "`hr_plan'" as txt "]"
        if `"`hr_artifacts'"' != "" {
            di as txt "helprun: artifacts saved: " as res `"`hr_artifacts'"'
        }
    }
    else {
        di as err `"`hr_message'"'
        di as txt "helprun: " as err "`hr_status'" ///
            as txt " / " as err "`hr_failure_class'" ///
            as txt " / " as err "`hr_reason'"
    }

    if `"`hr_logfile'"' != "" {
        di as txt "helprun: log saved to " as res `"`hr_logfile'"'
    }

    sreturn local status  `"`hr_status'"'
    sreturn local reason  `"`hr_reason'"'
    sreturn local logfile `"`hr_logfile'"'
end
