# helprun

**Run complete Stata help examples with a single click in the Viewer.**

`helprun` lets you open a Stata help page, prepare its runnable examples, and then run the complete example you choose by clicking **Run this example** in the Viewer. It is designed to avoid manual copy/paste and reconstruction of multi-line example code while protecting the parent Stata session.

## Installation

Install the current version directly from the GitHub repository:

```stata
net install helprun, from("https://raw.githubusercontent.com/Louis8102/helprun/main") replace
```

GitHub repository:

https://github.com/Louis8102/helprun

After installation, verify which files Stata is using:

```stata
which helprun
findfile _helprun.py
```

If Stata reports an older copy from another ado directory, remove or replace that stale copy before using the newly installed version.

## Basic use

Use `helprun` in three steps:

```stata
help topic
helprun
```

Then, in the temporary Viewer, find the example you want and click **Run this example** at the end of that example.

For example:

```stata
help regress
helprun
```

`helprun` does not execute an example merely because you type `helprun`. Execution begins only after you click a specific **Run this example** link.

## Key features

- **One-click complete examples.** Run complete examples from official Stata help and installed user-written help without rebuilding the code manually.
- **Protected execution with clear diagnostics.** Examples run outside the interactive parent session. If required data, runtime components, or other prerequisites are unavailable, `helprun` reports the reason instead of silently guessing.
- **Automatic output preservation.** Run output is returned to Results and a plain-text log is saved in the working directory current when the example is clicked. Capturable Stata graphs are preserved as `.gph` and `.png`. Clearly authored outputs can include `.dta`, `.csv`, `.xlsx`, `.docx`, `.pdf`, `.tex`, `.html`, and `.svg`. Existing files are not overwritten; when needed, `-run-2`, `-run-3`, and later suffixes are used.

Typical log names look like:

```text
regress-example-2.log
nestpreserve-example-1.log
```

## Examples

### Official Stata help

```stata
help regress
helprun
```

Choose any runnable example in the Viewer and click **Run this example**.

### Installed user-written help

```stata
help nestpreserve
helprun
```

`helprun` prepares the examples found in the installed help file in the same way.

### Help containing existing Stata command links

```stata
help reg2docx
helprun
```

Existing native Stata links are preserved, while `helprun` adds one **Run this example** control for the complete structural example.

### Delegated or shared help content

```stata
help sem
helprun
```

`helprun` can follow help content that is resolved through delegated or shared help files.

## Compatibility

Version 1.0.0 has been validated on:

- Windows 10
- StataNow 19.5
- Stata's Python integration

Other operating systems and Stata releases are not claimed by this validation.

## Version

**1.0.0**

## Author

Hao Ma, Ph.D.  
Email: shouhuoxiwang2027@gmail.com

## Suggested citation

If you use `helprun` in research, please cite:

Ma, H. (2026). *helprun: Run complete Stata help examples with a single click in the Viewer*. Statistical Software Components **S______**, Boston College Department of Economics. Version 1.0.0.

## License

`helprun` is free software licensed under the GNU General Public License version 3 (GPL-3.0).
