# helprun — validation summary

This is the portable summary of what has been validated and how. It carries no
machine-specific paths, so it travels with the release. The full evidence — one
frozen oracle and one results document per gate — is kept in the development
tree and is not part of this package.

helprun 2.0.0 was developed against a frozen nine-gate specification. Each gate
freezes its oracle *before* its cases run, and a gate cannot be closed by
redefining what it asked for.

## How to reproduce

Set the working directory to the repository root and run the single entry point:

```stata
cd <repository root>
do test_helprun.do
```

or, from anywhere:

```stata
global HELPRUN_PROJECT "<repository root>"
do <repository root>/test_helprun.do
```

One invocation runs the whole automated suite and prints one consolidated
result. Some cases need a real help Viewer, which batch mode does not provide,
so the entry point launches short GUI Stata sub-runs and merges their results.
It locates Stata itself; `global HELPRUN_STATA_EXE` overrides that.

Requirements: Stata 16 or newer for Windows, with Stata's Python integration
working (`python query`). The deterministic real-help corpus is the longest part
of the run; `global HELPRUN_SKIP_CORPUS "1"` skips it while iterating, and a
release candidate is never validated with it skipped.

## What each gate established

**GATE 1 — Specification.** The specification was read completely and frozen.
Every hard requirement maps to at least one observable case. No two requirements
contradict each other. The public UX requires no typed example number, and bare
`helprun` performs preparation only.

**GATE 2 — Independent reference.** Twenty-one reference findings were
established directly against Stata and Windows, independently of helprun's own
code: how SMCL renders, how `help` behaves in batch mode, that `EnumWindows`
order is true Z-order for one process's windows, that terminating a process does
not terminate its descendants, and that both CRLF and LF conventions occur in
the real help corpus.

**GATE 3 — Unit.** 107 cases covering source resolution, the decoding ladder,
the parser, click identity, the planner, the guard, the executor, the output
layer and the lifecycle. 107 PASS, 0 FAIL, 0 BLOCKED.

**GATE 4 — Adversarial.** 51 cases built to break it: a help file over 1 MiB,
120 examples in one file, hostile filenames, unwritable output directories,
concurrent clicks, foreign Viewers belonging to another Stata process, and
examples that deliberately end their own Stata process. 51 PASS, 0 FAIL,
0 BLOCKED. Every case additionally asserts that the parent session is intact,
that the stated reason is the strongest one the evidence supports, and that no
failure is misreported as an internal error.

**GATE 5 — Mutation.** 30 mutants were injected into production one at a time,
each with its detectors named in advance. 30 killed, 0 survivors, every mutation
restored and the restoration verified by hash. Production is byte-identical
before and after.

**GATE 6 — Integration.** 17 named scenarios, plus an exhaustive scan of every
help file in the installed BASE and PLUS trees, plus a deterministic corpus of
70 BASE and 30 PLUS topics selected by a frozen hash rule and actually executed.
17/17 scenarios, 0 corpus invariant failures, and 0 unexplained candidates.

The corpus scan is deliberately independent: it flags candidate help files
without calling helprun's parser, so production cannot quietly exclude its own
failures. On its first run it reported 51 unexplained candidates, every one of
which traced to a real defect. Among them: an over-broad rule that disabled 47
real help files, tab-indented code being invisible, three unrecognised
Examples-section title forms, and a planner that checked variables against only
the first of several datasets an example loads.

**GATE 7 — Regression.** Every mandatory case set re-run on the release
candidate, plus 20 standing regression cases — one for each reproducible defect
the earlier gates found — plus the corpus re-executed and compared against the
frozen GATE 6 snapshot.

```text
GATE 3 unit cases                      107 / 107
GATE 4 adversarial cases                51 /  51
GATE 5 detectors on unmutated code      58 /  58
GATE 6 integration scenarios            17 /  17
historical bug regression HR-01..HR-20  20 /  20
permanent production and help invariants 13 /  13
corpus comparison                        4 /   4   (0 regressions)
```

Every historical-regression fixture is synthetic and carries a renamed topic, so
no case can pass because a real topic name found its way into production. A
permanent invariant scans production for topic-specific branches on every run.

GATE 7 found and repaired two further production defects, which is what it is
for. One: a `{cmd:…}` span containing an SMCL brace encoding kept its markup, so
the command reached Stata as `{cmd:…}` and the run was reported as the help
author's error rather than helprun's. Two: an alternative-branch separator inside
an open `program`, `input`, Mata or `#delimit ;` block split that block and left
an example with no terminator; such an example is now refused rather than run.

**GATE 8 — Artifact inspection.** The actual release archives are inspected, not
the staging folder: exact member lists, the SSC package boundary of exactly three
runtime files, byte-identical production files across both archives, a scan for
credentials and machine-specific paths, and a clean-room test that installs the
extracted SSC archive on an isolated ado path and proves the packaged runtime
loads and runs an example with no help from the development tree.

**GATE 9 — Audit.** Audit-only. It synthesises the evidence and does not patch
code, help, tests or archives.

## One checkpoint is human

Two behaviours resist ordinary batch testing, because both need a real Stata
Viewer on a real desktop:

- that several **real** Stata help Viewers can be open in one process, and the
  current one is the one selected;
- that the temporary Viewer visibly **renders** the run link, and that clicking
  it launches the exact example shown.

The **first is now automated.** On this Stata the Viewer is a single MDI frame:
each `help` adds an inner document, not a window, so no Stata command produces
a second Viewer window — twenty-three were measured and none did. The Viewer's
own `File > New window` menu command does, and it is driven by an external
process sending that command's accelerator. What the test then asserts is
unchanged: two genuinely real Viewer windows, counted with the same pattern
production uses, and the run refuses to record a pass unless they exist.
Automating how the condition is created is not the same as weakening what is
checked.

The **second remains human**, and is the single consolidated manual checkpoint
the specification allows. No machine can confirm that a link was visibly
rendered.

Both are recorded with the SHA-256 of the engine they were taken against, so a
checkpoint cannot be silently reused for a build it never tested. The click
checkpoint is artifact-backed rather than self-reported: the output directory is
inventoried before the click, and the result is decided afterwards from the log
that appeared — its name, timestamp, header, example ordinal and command count.

## Limits of this validation

- Windows only. Identifying the help Viewer that belongs to a Stata process uses
  Windows-specific facilities.
- Validated against one Stata release, StataNow 19.5 MP. Help content differs
  between Stata versions, so example counts for a given topic can differ too.
- The corpus is a deterministic 100-topic sample of the installed trees, not
  every help file. The exhaustive static scan does cover every file.
- A refusal is a valid outcome. Many real help examples need your own data, a
  licensed dataset, a network resource or a decision that is not helprun's to
  make. helprun names the reason instead of guessing.
