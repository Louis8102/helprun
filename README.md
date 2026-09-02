# helprun

Run a Stata help example by clicking it in the Viewer.

Open a help page and type `helprun`. A temporary copy of that page opens in the
Viewer with a **Run this example** link under every example helprun can run.
Clicking one link runs exactly that example in a hidden child Stata process and
brings its output back to your Results window.

```stata
help regress
helprun
```

Then click the link under the example you want.

No code is copied into the Do-file Editor and no example number is typed. The
example is chosen by clicking it, so what runs is the example you were looking
at. The original help file is never modified, and your interactive session keeps
its data, its working directory and its estimation results.

## What it does

- Reconstructs the whole example: multiline commands, continuation lines, `.`
  prompts, loops, `program`/`input`/Mata blocks and `#delimit ;` sections.
- Runs the earlier setup examples the clicked one actually needs, and no others.
- Isolates the run: a hidden child Stata with a private working directory, so
  the example's `clear`, `cd` and file writes do not reach your session.
- Refuses rather than guesses. An example that cannot be reconstructed
  unambiguously, or that needs your data, a licence, a credential or a decision,
  is refused with a stated reason.
- Keeps the output: the run appears in Results, and its log, graphs and any files
  the example created are saved to the working directory you clicked from.

## Requirements

- Stata 16 or newer, for Windows.
- Stata's Python integration working. Check with `python query`.

Identifying the help Viewer that belongs to your Stata process uses
Windows-specific facilities, so helprun is Windows-only. See the Known
limitations section of `help helprun` for the full list.

## Install

Copy the three runtime files onto your ado path:

```text
helprun.ado
_helprun.py
helprun.sthlp
```

Then `help helprun` for the full documentation.

## Repository layout

```text
helprun.ado          the command
_helprun.py          the engine, run inside Stata's Python
helprun.sthlp        the help file
test_helprun.do      the single master validation entry point
tests/               the automated acceptance suite
fixtures/            controlled help fixtures used by the suite
VALIDATION.md        what has been validated, and how
```

To run the suite, set the working directory to the repository root and run the
entry point, or point it at the root explicitly:

```stata
cd <repository root>
do test_helprun.do
```

```stata
global HELPRUN_PROJECT "<repository root>"
do <repository root>/test_helprun.do
```

Some cases need a real help Viewer, which batch mode does not provide, so the
entry point launches short GUI Stata sub-runs and merges their results. It
locates Stata itself; `global HELPRUN_STATA_EXE` overrides that if needed.

## Version

2.0.0

## Author

Hao Ma, PhD — shouhuoxiwang2027@gmail.com

## License

helprun is free software licensed under the GNU General Public License version 3
(GPL-3.0).
