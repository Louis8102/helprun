{smcl}
{* *! version 1.0.0 06sep2026}{...}
{vieweralsosee "help" "help help"}{...}
{vieweralsosee "view" "help view"}{...}
{vieweralsosee "python" "help python"}{...}

{title:Title}

{phang}
{bf:helprun} {hline 2} Run code examples from official and user-written Stata help files by clicking in the Viewer{p_end}

{title:Description}

{pstd}
{cmd:helprun} lets you run a code example from a Stata help file without copying it into the Do-file Editor. The example runs outside your interactive session, and its log, code, output files, and a run summary are saved automatically beneath the current Stata working directory.{p_end}

{title:Why use helprun?}

{pstd}
Help-file examples can be long and inconvenient to copy and run manually. Many also finish without preserving the code or output. {cmd:helprun} makes the example easier to execute and keeps the resulting run materials together for later review and reuse.{p_end}

{title:Key features}

{p 4 8 2}• {bf:Run this example.} Open a help topic, type {cmd:helprun}, and click the desired example in the temporary Viewer.{p_end}
{p 4 8 2}• {bf:Reconstruct.} {cmd:helprun} preserves authored command order and adds only setup that is required and supported by evidence.{p_end}
{p 4 8 2}• {bf:Isolate.} The selected example runs outside the interactive dataset; ambiguous or unsupported execution is refused rather than guessed.{p_end}

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

{pstd}After each run, Results reports the directory where the run files were saved. Existing files are not overwritten; repeated runs use {cmd:-run-2}, {cmd:-run-3}, and later suffixes.{p_end}

{title:Practical applications}

{pstd}
The examples below are real topics on a system where {cmd:helprun} has been
tested. They show a long third-party example that creates multiple output
files, an ordinary official Stata help topic, a third-party topic written as
native clickable commands, and a topic whose example cannot run because the
help file supplies no data.{p_end}

{title:Example 1. Running a long nestpreserve example with multiple saved outputs}

{p 4 4 2}{cmd:help nestpreserve}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example 4.{p_end}

{pstd}Example 4 downloads county homicide data, performs a multi-part spatial and temporal analysis, and creates several output files. A successful run includes {cmd:County_Homicide_Rates_1960_1990.pdf}, {cmd:County_Homicide_Rates_1960_1990.png}, {cmd:Average_County_Homicide_Rate_Trend.pdf}, {cmd:Average_County_Homicide_Rate_Trend.png}, and the downloaded data files. {cmd:helprun} also saves the run materials and preserves capturable Stata graphs as .gph and .png files in the {cmd:nestpreserve} directory beneath the current Stata working directory. Existing files are not overwritten; repeated runs use {cmd:-run-2}, {cmd:-run-3}, and later suffixes.{p_end}

{title:Example 2. Running an official Stata help example}

{p 4 4 2}{cmd:help regress}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example 2.{p_end}

{pstd}
The {cmd:regress} help page offers four runnable examples. Example 2 is the
robust standard errors example, which loads its own data and then fits several
models. Results shows the commands and their output as if you had typed them,
and the run is saved in a {cmd:regress} directory beneath your working
directory.{p_end}

{title:Example 3. Running a third-party example written as clickable commands}

{p 4 4 2}{cmd:help reg2docx}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example.{p_end}

{pstd}
This help page writes its whole example as a long sequence of individually
clickable commands. Those original blue links are left exactly as the author
wrote them; {cmd:helprun} simply adds one control that runs the example as a
single unit, in the authored order, rather than making you click twenty-four
commands one at a time.{p_end}

{title:Example 4. Running an example that requires user confirmation}

{p 4 4 2}{cmd:help varorder}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example 1.{p_end}

{pstd}When {cmd:varorder_example_data.dta} is available in the working directory, the {cmd:varorder} example displays a preview and asks the user to press Enter before applying the proposed variable ordering. The prompt appears in Results, and the user answers in the Command window; {cmd:helprun} never answers automatically.{p_end}

{title:Example 5. Running an example that does not provide the required data}

{p 4 4 2}{cmd:help minvar}{p_end}
{p 4 4 2}{cmd:helprun}{p_end}
{pstd}Click the "Run this example" icon for Example 1.{p_end}

{pstd}The {cmd:minvar} help example does not provide the required data. Therefore, when the example is run, {cmd:helprun} reports: {bf:helprun: this example does not provide a runnable dataset or data setup.}{p_end}

{title:Compatibility}

{pstd}{cmd:helprun} requires Stata 16 or later with Stata's Python integration enabled. Release validation was performed on Windows 10 with StataNow 19.5.{p_end}

{pstd}
{cmd:helprun} is {bf:not} a malware sandbox. Its isolation is for the Stata data/session boundary; it does not sandbox the host computer from commands authored by an example. Installing or configuring software is never done silently by {cmd:helprun}; such actions can occur only when they are explicitly authored by the example being run.{p_end}

{pstd}
Compatibility beyond the platforms and Stata releases covered by this release's validation is not validated.{p_end}

{title:Version}

{pstd}
1.0.0{p_end}

{title:Author}

{pstd}
Hao Ma, PhD{p_end}

{pstd}
Email: {browse "mailto:shouhuoxiwang2027@gmail.com":shouhuoxiwang2027@gmail.com}{p_end}

{title:Suggested citation}

{pstd}
Ma, H. (2026). helprun: Run code examples from official and user-written Stata help files by clicking in the Viewer. Version 1.0.0.{p_end}

{title:License}

{pstd}
{cmd:helprun} is free software licensed under the GNU General Public License
version 3 (GPL-3.0).{p_end}
