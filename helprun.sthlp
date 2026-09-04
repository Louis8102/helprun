{smcl}
{* *! version 1.0.0 03sep2026}{...}
{vieweralsosee "help" "help help"}{...}
{vieweralsosee "view" "help view"}{...}
{vieweralsosee "python" "help python"}{...}

{title:Title}

{phang}
{bf:helprun} {hline 2} Run a complete Stata help example by clicking it in the Viewer{p_end}

{title:Description}

{pstd}
{cmd:helprun} lets you run the examples you are reading in a Stata help file
without copying anything into the Do-file Editor. Open any help topic, type
{cmd:helprun}, and a temporary copy of that help page appears with a
{bf:Run this example} control beside each complete example. Click the one you
want and it runs.{p_end}

{pstd}
The help file you are reading is never modified, and the example never runs in
your session. Each click runs in a separate hidden Stata, so your data, your
results and your working directory are left exactly as they were.{p_end}

{pstd}
Each click saves its output. {cmd:helprun} creates a directory named for the
help topic beneath your current working directory and writes the run log there,
together with any graphs the example produced and any file it clearly set out
to create. Existing files are never overwritten: a second run of the same
example is saved alongside the first under a {cmd:-run-2} name. After the run,
Results prints one line naming that directory:{p_end}

{pstd}{bf:helprun: log and other output files saved in} {it:directory}{p_end}

{title:Why use helprun?}

{pstd}
Help examples are written to be read, not to be run. To try one you normally
have to select the right lines, strip the leading dots and continuation
markers, notice that the example depends on a dataset loaded three paragraphs
earlier, paste the result somewhere, and hope you did not disturb the data you
already had open.{p_end}

{pstd}
{cmd:helprun} removes that work. It reads the example as the author wrote it,
adds only the earlier setup that example actually needs, and runs the whole
thing in one step. When an example cannot run, it tells you plainly why
instead of failing in a way you have to diagnose yourself.{p_end}

{title:Key features}

{p 4 8 2}• {bf:One click, whole example.} Run a complete structural example straight from
the Viewer, with no copying, no editing and no example number to type.{p_end}

{p 4 8 2}• {bf:Reads real help files.} Reconstructs examples conservatively
across the formats Stata help actually uses, including continuation lines,
blocks, native clickable command links, and setup written earlier in the page.{p_end}

{p 4 8 2}• {bf:Isolated and saved.} Runs in a hidden Stata that cannot touch
your session, preserves the log, graphs and authored output, and reports a
clear, evidence-based reason when an example cannot run.{p_end}

{title:Syntax}

{p 4 4 2}
{cmd:helprun}{p_end}

{pstd}
{cmd:helprun} takes no options. The ordinary sequence is{p_end}

{p 8 8 2}{cmd:help} {it:topic}{p_end}
{p 8 8 2}{cmd:helprun}{p_end}
{p 8 8 2}then click the {bf:Run this example} control beside the example you want.{p_end}

{pstd}
Typing {cmd:helprun} only prepares the clickable view. Nothing is executed,
downloaded or installed until you click a specific example.{p_end}

{title:Practical applications}

{pstd}
The examples below are real topics on a system where {cmd:helprun} has been
tested. They show an ordinary official help topic, a third-party topic whose
example is written as native clickable commands, and a topic whose example
cannot run because the help file supplies no data.{p_end}

{title:Example 1. Running an official Stata help example}

{p 4 4 2}{cmd:help regress}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example 2.{p_end}

{pstd}
The {cmd:regress} help page offers four runnable examples. Example 2 is the
robust standard errors example, which loads its own data and then fits several
models. Results shows the commands and their output as if you had typed them,
and the run is saved in a {cmd:regress} directory beneath your working
directory.{p_end}

{title:Example 2. Running a third-party example written as clickable commands}

{p 4 4 2}{cmd:help reg2docx}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example 1.{p_end}

{pstd}
This help page writes its whole example as a long sequence of individually
clickable commands. Those original blue links are left exactly as the author
wrote them; {cmd:helprun} simply adds one control that runs the example as a
single unit, in the authored order, rather than making you click twenty-four
commands one at a time.{p_end}

{title:Example 3. An example the help file cannot supply data for}

{p 4 4 2}{cmd:help minvar}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example 1.{p_end}

{pstd}
The {cmd:minvar} help page shows its command applied to variables such as
{cmd:anx1_1} and {cmd:anx2_1}, but the page never loads a dataset and never
generates one, so there is nothing for the example to run against. Rather than
inventing data or reporting an obscure error, {cmd:helprun} says so:{p_end}

{pstd}{bf:helprun: this example does not provide a runnable dataset or data setup.}{p_end}

{pstd}
A diagnostic log is still written to the {cmd:minvar} directory, and the
location is printed in Results. This is the expected outcome for an example
that documents a command's syntax without providing data, and it is not a
failure of the help file or of your setup.{p_end}

{title:Compatibility}

{pstd}
{cmd:helprun} runs each example in a hidden Stata so your session is
protected, but it is {bf:not} a malware sandbox and it makes no security
guarantee about code an author wrote. Installing or downloading anything on your
behalf is never done silently: whatever would change your Stata installation
is described first and needs your confirmation.{p_end}

{pstd}
{cmd:helprun} has been developed and tested on Windows 10 with StataNow 19.5
and Stata's Python integration available ({cmd:python query}). Identifying the
help Viewer belonging to your Stata session uses Windows facilities. Other
platforms and other Stata versions are not validated, and no wider
compatibility is claimed.{p_end}

{title:Version}

{pstd}
1.0.0{p_end}

{title:Author}

{pstd}
Hao Ma, PhD{p_end}

{pstd}
Email: {browse "mailto:shouhuoxiwang2027@gmail.com":shouhuoxiwang2027@gmail.com}{p_end}

{title:License}

{pstd}
{cmd:helprun} is free software licensed under the GNU General Public License
version 3 (GPL-3.0).{p_end}
