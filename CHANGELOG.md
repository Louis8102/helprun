# Changelog

All notable changes to helprun are recorded here. Versions follow
[Semantic Versioning](https://semver.org).

## 1.0.0 — 2026-09-03

First public release.

### Added

- **Click to run.** `help <topic>` then `helprun` prepares a temporary
  clickable copy of the help page with one **Run this example** control beside
  each complete structural example. Clicking one runs that example.
- **Conservative reconstruction** across the formats real Stata help uses:
  `.sthlp`, legacy `.hlp`, recursive `INCLUDE`/`.ihlp` fragments, delegated and
  shared help, prompt and continuation lines, `#delimit ;`, `program`, `input`
  and Mata blocks, and examples written entirely as native clickable commands.
- **Only the setup an example needs.** Earlier setup in the page is added when
  the clicked example actually depends on it, in the authored order, rather
  than replaying every preceding example.
- **Isolated execution.** Every click runs in a hidden Stata with its own
  private temporary state. The parent dataset, results and working directory
  are unchanged.
- **Saved output.** A directory named for the root help topic is created
  beneath the click-time working directory and holds the run log, every
  capturable Stata graph as `.gph` and `.png` in creation order, and clearly
  authored final artifacts. Existing files are never overwritten; a repeat run
  is saved under a `-run-k` name.
- **Concise Results.** The parent Results window shows the authored commands
  and their ordinary Stata output once, followed by a single line giving the
  absolute directory the run was saved in. Provenance, internal classification
  and the full transcript remain in the log.
- **Evidence-based diagnostics.** When an example cannot run, helprun reports
  the strongest explanation the evidence supports rather than guessing. An
  example that documents a command's syntax without providing any dataset or
  data-generating setup is reported as such, in plain language, instead of
  surfacing an obscure Stata error.

### Notes

- Windows only. Identifying the help Viewer belonging to a Stata process uses
  Windows facilities.
- Developed and validated against StataNow 19.5 on Windows 10, with Stata's
  Python integration available. No wider compatibility is claimed.
- helprun documents no public `r()` interface in this release.
