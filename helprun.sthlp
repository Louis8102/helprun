{smcl}
{* *! version 2.0.0 01sep2026}{...}
{vieweralsosee "help" "help help"}{...}
{vieweralsosee "view" "help view"}{...}
{vieweralsosee "do" "help do"}{...}
{vieweralsosee "python" "help python"}{...}

{title:Title}

{phang}
{bf:helprun} {hline 2} Run a Stata help example by clicking it in the Viewer{p_end}

{title:Description}

{pstd}
HELPRUN runs the example you are reading. Open a help page and type
{cmd:helprun}: a temporary copy of that page opens in the Viewer, and every
example helprun can run gains a {bf:Run this example} link. Clicking one link
runs exactly that example in a hidden child Stata process and brings its output
back to your Results window.{p_end}

{pstd}
No code is copied into the Do-file Editor and no example number is typed. The
example is chosen by clicking it, so what runs is the example you were looking
at. The original help file is never modified, and your interactive session keeps
its data, its working directory, and its estimation results.{p_end}

{title:Key features}

{p 4 8 2}• {bf:Click, do not copy.} The link sits under the example itself, so no
code, setup line, or data command has to be selected and pasted anywhere.{p_end}
{p 4 8 2}• {bf:The whole example.} Multiline commands, continuation lines,
{cmd:.} prompts, loops, {cmd:program}/{cmd:input}/Mata blocks, and
{cmd:#delimit ;} sections are reconstructed as the author wrote them.{p_end}
{p 4 8 2}• {bf:Setup included.} When the clicked example depends on setup shown
in an earlier example, helprun first runs only the earlier examples it actually
needs.{p_end}
{p 4 8 2}• {bf:Session isolation.} The example runs in a hidden child Stata with
a private working directory, so its {cmd:clear}, {cmd:cd}, and file writes do
not reach your session.{p_end}
{p 4 8 2}• {bf:Evidence, not guessing.} An example that cannot be reconstructed
unambiguously, or cannot be run without your decision, is refused with a stated
reason instead of being run on a guess.{p_end}
{p 4 8 2}• {bf:The output is kept.} The run appears in the Results window, and
its log, graphs, and any files the example created are saved next to your
work.{p_end}

{title:Syntax}

{p 4 4 2}{cmd:helprun}{break}
Prepare the help page currently open in this Stata's Viewer. helprun resolves
the help source through Stata, reconstructs each example, and opens a temporary
clickable copy in the Viewer. This step executes nothing: no example runs, no
child Stata starts, nothing is downloaded, and nothing is written outside
Stata's temporary directory. There are no options, and there is deliberately no
example number to type: the example is identified by the link you click.{p_end}

{title:Getting started}

{pstd}
Read a help page and run one of its examples:{p_end}

{p 4 4 2}help regress{p_end}
{p 4 4 2}helprun{p_end}

{pstd}
The Viewer shows the same page you were reading, with its own blue help links
intact, and a {bf:Run this example} link under each runnable example. helprun
also reports in the Results window how many runnable examples it found. Click
the link under the example you want; its commands run, its output is printed in
the Results window, and the log is saved to your current working directory.{p_end}

{pstd}
Type {cmd:helprun} again at any time to rebuild the clickable copy from the
current help source.{p_end}

{title:How helprun decides what to run}

{pstd}
helprun does not search for a help file by name. It reads the topic from the
help Viewer belonging to this Stata process, asks Stata to resolve that topic,
and then follows only the help links the author actually wrote, so a delegated
or shared help page is read where the author pointed rather than guessed
at.{p_end}

{pstd}
Every file that contributes to the page you are reading, the main help file and
each {cmd:INCLUDE help} or {cmd:.ihlp} fragment it pulls in, is recorded, and
the link under an example carries an identity derived from that whole set of
sources together with the example's position in it. Clicking a link therefore
runs the example as it was when the page was prepared. If the help source
changes underneath a clickable view, the click is refused rather than run
against text you never saw.{p_end}

{pstd}
Only clicking a link runs anything, and each click runs exactly one example plus
the earlier setup examples that example needs.{p_end}

{title:What you see and what is saved}

{pstd}
The clicked example runs in a hidden child Stata. When it finishes, its log is
printed in your Results window, so estimation tables, summary statistics,
displayed output, and any Stata error messages are visible without opening a
file.{p_end}

{pstd}
The log is also saved to the working directory that was current when you
clicked, as{p_end}

{p 8 8 2}{it:topic}{cmd:-example-}{it:N}{cmd:.log}{p_end}

{pstd}
where {it:N} is the position of the example in the help page. Graphs the example
drew are saved beside it as
{it:topic}{cmd:-example-}{it:N}{cmd:-graph-}{it:k} in {cmd:.gph} and {cmd:.png}
form, and data or document files the example itself created are copied out under
their own names. helprun never overwrites an existing file and never rotates or
deletes one: if a name is already taken, the whole set of files for that run is
written under {it:topic}{cmd:-example-}{it:N}{cmd:-run-}{it:k} instead.{p_end}

{pstd}
Everything else the run produced stays in the temporary sandbox and is removed
with it.{p_end}

{title:When helprun refuses}

{pstd}
A click ends in one of three states: {cmd:SUCCESS}, {cmd:FAILED} when execution
was attempted and did not succeed, or {cmd:REFUSED} when helprun declined to
attempt it. A run that is not a success always reports the class of the problem
and a specific reason, so the message says what stood in the way rather than
that something went wrong.{p_end}

{synoptset 16 tabbed}{...}
{synopt:{cmd:SOURCE}}the help page itself could not be read, resolved, or
trusted{p_end}
{synopt:{cmd:EXAMPLE}}the authored example has no runnable code, could not be
reconstructed unambiguously, or its own code failed{p_end}
{synopt:{cmd:DEPENDENCY}}data, a package file, an earlier prerequisite, or a
network resource the example needs is not available{p_end}
{synopt:{cmd:RUNTIME}}the example needs a Stata version, platform, external
application, licence, or credential that is not present{p_end}
{synopt:{cmd:SAFETY}}the example asks for something helprun will not do on its
own, or that requires your decision first{p_end}
{synopt:{cmd:EXECUTION}}the run could not be carried out as one isolated
execution, for example a timeout or a dependency on state from another
process{p_end}
{synopt:{cmd:OUTPUT}}the log or an artifact could not be written where it
belongs{p_end}
{synopt:{cmd:INTERNAL}}a defect in helprun itself{p_end}

{pstd}
Refusals are ordinary and expected. Real help files show examples that need your
own data, a licensed dataset, a network resource, or a decision that is not
helprun's to make, and naming the reason is more useful than running something
approximate.{p_end}

{title:Safety}

{pstd}
Help files belonging to Stata and to other packages are read only. helprun
writes its clickable copy to a temporary file of its own and never edits the
help source it read.{p_end}

{pstd}
Each click gets a fresh private working directory and a private temporary
directory, and the example runs in a hidden child Stata process with no console
window of its own. Every child process belonging to one click shares that
private state; separate clicks and your own session do not. If an authored
example deliberately ends a Stata process with a top-level {cmd:exit} and
continues afterwards, helprun treats that as a process boundary: the first child
is allowed to end and the remaining commands continue in a fresh child. Your
interactive Stata is never the process that exits.{p_end}

{pstd}
Whether a command may run is decided by what the command is for and where its
target came from, not by a list of forbidden file extensions. An installed
package component that an example legitimately calls is treated as a package
component; something with no such provenance is not, whatever it is called.
Installing, updating, or persistently reconfiguring anything is never done
silently: those commands stop the run and are reported as needing your
confirmation, so nothing is added to your Stata installation behind your
back.{p_end}

{pstd}
A hidden child Stata protects your session from the ordinary accidents of
running example code. It is {bf:not} an operating-system or malware sandbox, and
it is not a security boundary. Do not use {cmd:helprun} as a way to execute help
files or ado-code you have reason to distrust.{p_end}

{title:Supported help content}

{pstd}
helprun reads Stata {cmd:.sthlp} files, legacy {cmd:.hlp} files, and the
{cmd:INCLUDE help} and {cmd:.ihlp} fragments they include, and it follows
authored delegation to a shared help page. Within a page it recognises examples
presented as plain indented code, as SMCL command or input spans, and as
numbered paragraph blocks such as {cmd:{c -(}p 4 4 2{c )-}}, with or without a
leading {cmd:.} prompt, whether the code is indented with spaces or with
tabs.{p_end}

{pstd}
Reconstruction rejoins continuation lines, keeps repeated commands in order, and
holds blocks such as {cmd:program} ... {cmd:end}, {cmd:input} ... {cmd:end},
Mata sections, and {cmd:#delimit ;} sections together. Where an author offers
genuine alternatives for the same task, each alternative becomes its own
clickable example rather than being run as one sequence.{p_end}

{title:Known limitations}

{p 4 8 2}• {bf:Windows.} helprun runs on Stata for Windows. Identifying the help
Viewer that belongs to your Stata process uses Windows-specific facilities and
has no equivalent on other platforms.{p_end}
{p 4 8 2}• {bf:Python integration.} Stata's Python integration must be working,
since helprun's engine runs inside it. Type {cmd:python query} to check.{p_end}
{p 4 8 2}• {bf:One process.} A help Viewer must be open in the same Stata
process. helprun cannot read a Viewer belonging to another Stata instance, and
it does nothing useful in batch mode, where {cmd:help} is ignored.{p_end}
{p 4 8 2}• {bf:One run at a time.} A click made while another click is still
running is refused rather than queued.{p_end}
{p 4 8 2}• {bf:Time limit.} A run that has not finished within a fixed time
limit is stopped, and the whole process tree it started is stopped with
it.{p_end}
{p 4 8 2}• {bf:What helprun cannot supply.} Examples that need your own data, a
licensed or credentialed resource, an external application, or a newer Stata
than yours are refused.{p_end}
{p 4 8 2}• {bf:Prose as much as code.} Some examples cannot have their commands
recovered unambiguously. Those are refused rather than approximated.{p_end}
{p 4 8 2}• {bf:No stored results.} helprun promises no {cmd:r()} results. Values
it happens to return are internal and may change without notice; do not write
code against them.{p_end}

{title:Compatibility}

{pstd}
HELPRUN requires Stata 16 or newer for Windows, with Stata's Python integration
available.{p_end}

{title:Author}

{pstd}
Hao Ma, PhD{p_end}

{pstd}
Email: {browse "mailto:shouhuoxiwang2027@gmail.com":shouhuoxiwang2027@gmail.com}{p_end}

{title:Version}

{pstd}
2.0.0{p_end}

{title:License}

{pstd}
helprun is free software licensed under the GNU General Public License version 3 (GPL-3.0).{p_end}
