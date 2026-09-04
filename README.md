# helprun

Run a Stata help example by clicking it in the Viewer.

Open a help page and type `helprun`. A temporary copy of that page opens in the
Viewer with a **Run this example** control beside every example helprun can run.
Clicking one runs exactly that example in a hidden Stata and brings its output
back to your Results window.

```stata
help regress
helprun
```

Then click the control beside the example you want.

No code is copied into the Do-file Editor and no example number is typed. The
example is chosen by clicking it, so what runs is the example you were looking
at. The original help file is never modified, and your interactive session keeps
its data, its working directory and its estimation results.

## Installation

```stata
net install helprun, from("https://raw.githubusercontent.com/Louis8102/helprun/main") replace
```

Then `help helprun` for the full documentation.

The package also ships `test_helprun.do`, the single validation entry point, so
you can reproduce the suite against the exact copy you installed. Stata treats a
do-file as an ancillary package member, so retrieve it with:

```stata
net get helprun, from("https://raw.githubusercontent.com/Louis8102/helprun/main")
```

It needs the `tests/` and `fixtures/` trees from this repository, and says so
plainly if you run it without them.

To install from a local copy instead, put the three runtime files on your ado
path:

```text
helprun.ado
_helprun.py
helprun.sthlp
```

## Using it

```stata
help regress
helprun
```

Click **Run this example** beside the example you want. That is the whole
workflow.

## What it does

- Reconstructs the whole example: multiline commands, continuation lines, `.`
  prompts, loops, `program`/`input`/Mata blocks and `#delimit ;` sections, and
  examples written entirely as native clickable commands.
- Runs the earlier setup the clicked example actually needs, and no more.
- Isolates the run: a hidden Stata with its own private temporary state, so the
  example's `clear`, `cd` and file writes never reach your session.
- Refuses rather than guesses. An example that cannot be reconstructed
  unambiguously, or that needs your data, a licence, a credential or a decision,
  is refused with a stated reason.
- Keeps the output: helprun creates a directory named for the help topic beneath
  the working directory you clicked from, and saves the run log, any graphs and
  any files the example set out to create there. Nothing is ever overwritten — a
  repeat run is saved alongside the first under a `-run-2` name. Results prints
  one line giving that directory.

## Requirements

- Windows.
- Developed and validated on Windows 10 with StataNow 19.5. Other Stata versions
  and platforms are not validated, and no wider compatibility is claimed.
- Stata's Python integration working. Check with `python query`.

Identifying the help Viewer that belongs to your Stata process uses
Windows-specific facilities, which is why helprun is Windows-only.

## Repository layout

```text
helprun.ado          the command
_helprun.py          the engine, run inside Stata's Python
helprun.sthlp        the help file
helprun.pkg          Stata package metadata for net install
stata.toc            package index for `net from`
test_helprun.do      the single master validation entry point
tests/               the automated acceptance suite
fixtures/            controlled help fixtures used by the suite
validation/          the public validation summary
CHANGELOG.md         release history
LICENSE              GPL-3.0
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

1.0.0

## Author

Hao Ma, PhD — shouhuoxiwang2027@gmail.com

## License

Copyright (C) 2026 Hao Ma

helprun is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE.

`LICENSE` is the official GPL-3.0 text, copied verbatim from
<https://www.gnu.org/licenses/gpl-3.0.txt> rather than reproduced, so it can be
checked byte-for-byte against that source.
