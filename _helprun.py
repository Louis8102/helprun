import base64
import hashlib
import itertools
import json
import os
import re
import subprocess
import shutil
import tempfile
import time
import zlib
from pathlib import Path

# ctypes is required only for the Windows Viewer/window layer and for locating
# the running Stata executable.  Importing it lazily keeps the source/parser
# layer importable by a Python build without _ctypes, so parser behaviour can
# be unit-tested outside Stata.  Inside Stata ctypes is always present.
try:
    import ctypes
except ImportError:  # pragma: no cover - only on a ctypes-less interpreter
    ctypes = None


# ============================================================
# Context
# ============================================================

class _LazyWinDLL(object):
    """Resolve user32/kernel32 on first use rather than at import time."""

    def __init__(self, name):
        self._name = name
        self._dll = None

    def __getattr__(self, attr):
        if self._dll is None:
            if ctypes is None:
                raise RuntimeError(
                    "helprun: ctypes is unavailable in this Python build; "
                    "the Windows Viewer layer cannot be used here"
                )
            self._dll = getattr(ctypes.windll, self._name)
        return getattr(self._dll, attr)


user32 = _LazyWinDLL("user32")
kernel32 = _LazyWinDLL("kernel32")


# ------------------------------------------------------------
# Descendant-aware child termination
#
# GATE 2 R19 proved that terminating a process on Windows does NOT terminate
# its descendants: a parent Stata was killed and its grandchild Stata kept
# running.  Killing only the direct child would therefore leave orphaned
# processes behind on timeout.  A Job Object gives the whole tree one handle
# that can be terminated atomically.
#
# The job deliberately does NOT set JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: on a
# normal successful run an example may legitimately have launched something the
# user wants to keep.  The tree is terminated only when helprun itself decides
# the run must be stopped.
# ------------------------------------------------------------

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


def create_child_job():
    """Create an unnamed Job Object, or None if jobs are unavailable."""
    if ctypes is None:
        return None

    try:
        handle = kernel32.CreateJobObjectW(None, None)
    except Exception:
        return None

    return handle or None


def assign_process_to_job(job, pid):
    """Place one spawned process, and thus its future descendants, in the job."""
    if not job or ctypes is None:
        return False

    proc = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid)
    )

    if not proc:
        return False

    try:
        return bool(kernel32.AssignProcessToJobObject(job, proc))
    finally:
        kernel32.CloseHandle(proc)


def terminate_job(job, exit_code=1):
    """Terminate every process in the job, descendants included."""
    if not job:
        return False

    try:
        return bool(kernel32.TerminateJobObject(job, exit_code))
    except Exception:
        return False


def close_job(job):
    if job:
        try:
            kernel32.CloseHandle(job)
        except Exception:
            pass

VIEWER_RE = re.compile(
    r"(?:^|\s-\s)Viewer\s*-\s*help\s+(.+?)\s*$",
    flags=re.IGNORECASE
)


def _pid(hwnd):
    pid = ctypes.c_ulong()
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _title(hwnd):
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(max(n + 1, 2))
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value.strip()


def _parse_viewer(hwnd, current_pid):
    if not hwnd:
        return None
    if _pid(hwnd) != current_pid:
        return None
    if not user32.IsWindowVisible(hwnd):
        return None

    title = _title(hwnd)
    m = VIEWER_RE.search(title)
    if not m:
        return None

    raw = m.group(1).strip()
    if "##" in raw:
        topic, anchor = raw.split("##", 1)
    else:
        topic, anchor = raw, None

    return {
        "hwnd": int(hwnd),
        "title": title,
        "topic": topic.strip(),
        "anchor": anchor,
    }


def current_viewer():
    current_pid = os.getpid()
    rows = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p
    )

    def cb(hwnd, lparam):
        parsed = _parse_viewer(hwnd, current_pid)
        if parsed is not None:
            rows.append(parsed)
        return True

    callback = EnumWindowsProc(cb)
    user32.EnumWindows(callback, 0)

    if not rows:
        raise HelprunError(
            "NO_ACTIVE_HELP_VIEWER",
            "helprun: no help Viewer belonging to this Stata session is open; "
            "type help for a topic first, then helprun",
        )

    # GATE 2 R07 proved that, restricted to the current process's visible
    # titled top-level windows, EnumWindows order is true Z-order. The first
    # eligible row is therefore the highest/currently active help Viewer.
    # Enumeration is read-only and changes no Z-order.
    return rows[0]


# ============================================================
# Stata bridge
#
# GATE 2 R21b established that production Python can obtain the authoritative
# Stata-resolved path by asking Stata itself:
#
#     SFIToolkit.stata('quietly capture findfile "<name>"')
#     Macro.getGlobal("r(fn)")            -- empty string means "not found"
#
# Specification section 4 makes that Stata-resolved path authoritative: a
# Python-side approximation of the adopath must never override it.  The Python
# search below therefore exists only for running outside Stata (offline parser
# unit tests); inside Stata it is not consulted.
# ============================================================

# ============================================================
# Failure taxonomy (specification section 9)
#
#   status        SUCCESS / FAILED / REFUSED
#   failure_class SOURCE EXAMPLE DEPENDENCY RUNTIME SAFETY EXECUTION OUTPUT
#                 INTERNAL
#   reason        a specific, evidence-backed reason from the frozen vocabulary
#
# The class is derived from the reason rather than passed separately, so a
# reason can never be reported under an inconsistent class.
# ============================================================

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_REFUSED = "REFUSED"

CLASS_SOURCE = "SOURCE"
CLASS_EXAMPLE = "EXAMPLE"
CLASS_DEPENDENCY = "DEPENDENCY"
CLASS_RUNTIME = "RUNTIME"
CLASS_SAFETY = "SAFETY"
CLASS_EXECUTION = "EXECUTION"
CLASS_OUTPUT = "OUTPUT"
CLASS_INTERNAL = "INTERNAL"

REASON_CLASS = {
    # SOURCE
    "HELP_FILE_SYNTAX_ERROR": CLASS_SOURCE,
    "SOURCE_CHANGED": CLASS_SOURCE,
    "SOURCE_ENCODING_UNRELIABLE": CLASS_SOURCE,
    "SOURCE_UNREADABLE": CLASS_SOURCE,
    "SOURCE_NAME_INVALID": CLASS_SOURCE,
    "NO_ACTIVE_HELP_VIEWER": CLASS_SOURCE,
    # EXAMPLE
    "NO_RUNNABLE_EXAMPLE": CLASS_EXAMPLE,
    "HELP_CODE_ERROR": CLASS_EXAMPLE,
    "AMBIGUOUS_EXAMPLE_RECONSTRUCTION": CLASS_EXAMPLE,
    # Section 9: a data-dependent Example that supplies no dataset, no
    # data-generating setup and no user-data instruction. It is a property of
    # the authored EXAMPLE, not an internal error, so it must be registered
    # here -- an unregistered reason would default to the INTERNAL class and
    # be reported as a helprun defect.
    "EXAMPLE_DATA_SETUP_MISSING": CLASS_EXAMPLE,
    # DEPENDENCY
    "DATA_FILE_MISSING": CLASS_DEPENDENCY,
    "HELP_DATA_MISMATCH": CLASS_DEPENDENCY,
    "USER_DATA_REQUIRED": CLASS_DEPENDENCY,
    "PACKAGE_FILE_MISSING": CLASS_DEPENDENCY,
    "UNRESOLVED_PREREQUISITE": CLASS_DEPENDENCY,
    "NETWORK_RESOURCE_UNAVAILABLE": CLASS_DEPENDENCY,
    # RUNTIME
    "RUNTIME_MISSING": CLASS_RUNTIME,
    "RUNTIME_VERSION_MISMATCH": CLASS_RUNTIME,
    "STATA_VERSION_INCOMPATIBLE": CLASS_RUNTIME,
    "PLATFORM_INCOMPATIBLE": CLASS_RUNTIME,
    "EXTERNAL_APPLICATION_MISSING": CLASS_RUNTIME,
    "CREDENTIAL_REQUIRED": CLASS_RUNTIME,
    "LICENSE_REQUIRED": CLASS_RUNTIME,
    # SAFETY
    "UNSAFE_OPERATION_REFUSED": CLASS_SAFETY,
    "USER_CONFIRMATION_REQUIRED": CLASS_SAFETY,
    # EXECUTION
    "EXECUTION_TIMEOUT": CLASS_EXECUTION,
    "CROSS_PROCESS_STATE_DEPENDENCY": CLASS_EXECUTION,
    "USER_INTERACTION_REQUIRED": CLASS_EXECUTION,
    "INTERACTIVE_INPUT_REQUIRED": CLASS_EXECUTION,
    "HELPRUN_BUSY": CLASS_EXECUTION,
    "EXAMPLE_CONTINUES_EARLIER_EXAMPLE": CLASS_EXECUTION,
    "AMBIGUOUS_FAILURE_PROVENANCE": CLASS_EXECUTION,
    # OUTPUT
    "OUTPUT_DIRECTORY_NOT_WRITABLE": CLASS_OUTPUT,
    "OUTPUT_ARTIFACT_MISSING": CLASS_OUTPUT,
    # INTERNAL
    "HELPRUN_INTERNAL_ERROR": CLASS_INTERNAL,
}


def failure_class_for(reason):
    """Map a frozen reason to its failure class.

    An unknown reason is INTERNAL by construction, which makes an unregistered
    reason visible as a helprun defect instead of silently masquerading as a
    legitimate classification.
    """
    return REASON_CLASS.get(reason, CLASS_INTERNAL)


def make_outcome(status, reason="", message="", **extra):
    """Build one taxonomy-consistent outcome record."""
    record = {
        "status": status,
        "failure_class": "" if not reason else failure_class_for(reason),
        "reason": reason,
        "message": message,
    }
    record.update(extra)
    return record


class HelprunError(Exception):
    """A failure carrying a frozen reason code; the class is derived from it."""

    def __init__(self, reason, message, detail=None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.detail = detail

    @property
    def failure_class(self):
        return failure_class_for(self.reason)

    def as_outcome(self, status=STATUS_REFUSED):
        return make_outcome(
            status, self.reason, self.message, detail=self.detail or ""
        )


class HelprunSourceError(HelprunError):
    """Retained name for source-layer failures; behaviour is HelprunError."""


def stata_available():
    try:
        import sfi  # noqa: F401
    except Exception:
        return False
    return True


def _stata_quote(text):
    # findfile takes a filename; a double quote cannot legally appear in a
    # Windows filename, so rejecting it is safe and avoids any injection.
    if '"' in text:
        raise HelprunSourceError(
            "SOURCE_NAME_INVALID",
            'helprun: illegal double quote in source name: ' + text,
        )
    return '"' + text + '"'


def stata_findfile(name):
    """Authoritative resolution. Returns Path, or None when Stata cannot find it."""
    from sfi import SFIToolkit, Macro

    SFIToolkit.stata(
        "quietly capture findfile " + _stata_quote(name)
    )

    fn = Macro.getGlobal("r(fn)")

    if not fn:
        return None

    return Path(os.path.normpath(fn.strip()))


def stata_local_dirs(stata_roots=None):
    """
    Adopath directories in the authoritative order established by GATE 2 R06:
    BASE, SITE, ".", PERSONAL, PLUS, OLDPLACE.

    Only used when running outside Stata.  Note that PERSONAL precedes PLUS;
    the previous implementation had them inverted.
    """
    dirs = []

    for raw in (stata_roots or []):
        if not raw:
            continue
        p = Path(raw)
        if p.exists() and p not in dirs:
            dirs.append(p)

    return dirs


# ============================================================
# Source decoding
#
# Frozen GATE 2 R20/R20b/R20c ladder.  Never errors="replace": 84 installed
# help files are genuinely not UTF-8 and some of their non-UTF-8 bytes sit
# inside documented command text, where a replacement character would silently
# change what the command does.
# ============================================================

BOM_UTF8 = b"\xef\xbb\xbf"


def decode_help_bytes(data):
    """Return (text, encoding_label).  Raises HelprunSourceError if undecodable."""
    body = data[len(BOM_UTF8):] if data.startswith(BOM_UTF8) else data

    try:
        return body.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # Windows-1252 is the legacy encoding Stata's own documentation names for
    # extended-ASCII files on Windows (unicode_encoding.sthlp), and it is the
    # only encoding consistent with the observed corpus byte distribution
    # (0x96 and 0x92 are 63% of all invalid bytes and are undefined in Latin-1).
    try:
        return body.decode("cp1252"), "cp1252-legacy"
    except UnicodeDecodeError as exc:
        raise HelprunSourceError(
            "SOURCE_ENCODING_UNRELIABLE",
            "helprun: help source could not be decoded reliably; "
            "it is neither valid UTF-8 nor valid Windows-1252",
            detail=str(exc),
        )


class SourceLine(str):
    """A help line that remembers which file and line number it came from.

    Subclassing str keeps every existing parser expression working unchanged
    while carrying the provenance the frozen source-graph identity needs.
    """

    __slots__ = ("src_path", "src_lineno")

    def __new__(cls, text, src_path, src_lineno):
        obj = super().__new__(cls, text)
        obj.src_path = src_path
        obj.src_lineno = src_lineno
        return obj


class SourceGraph(object):
    """Every file that contributes content to one parsed help topic."""

    def __init__(self):
        self.lines = []
        self.files = []          # ordered [{path, sha256, encoding, bytes}]
        self._seen = set()

    def add_file(self, path, digest, encoding, size):
        key = str(path).lower()
        if key in self._seen:
            return
        self._seen.add(key)
        self.files.append(
            {
                "path": str(path),
                "sha256": digest,
                "encoding": encoding,
                "bytes": size,
            }
        )

    @property
    def aggregate_hash(self):
        """Deterministic hash over the ordered contributing source graph.

        Covers every included .ihlp and delegated source, not only the root,
        so a change to any contributing file invalidates click identity.
        """
        h = hashlib.sha256()
        for entry in self.files:
            h.update(entry["path"].lower().replace("\\", "/").encode("utf-8"))
            h.update(b"\0")
            h.update(entry["sha256"].encode("ascii"))
            h.update(b"\0")
        return h.hexdigest()


def read_source_bytes(path):
    """Read one help file and return (lines, sha256, encoding)."""
    path = Path(path)

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HelprunSourceError(
            "SOURCE_UNREADABLE",
            "helprun: help source could not be read: " + str(path),
            detail=str(exc),
        )

    digest = hashlib.sha256(data).hexdigest()
    text, encoding = decode_help_bytes(data)

    # splitlines() handles CRLF (PLUS) and LF (BASE) alike; GATE 2 R20 showed
    # both conventions occur in the real corpus.
    return text.splitlines(), digest, encoding, len(data)


# ============================================================
# Persistent learning output
#
# Frozen naming (specification sections 12 and 16):
#   base            <topic>-example-<n>
#   on collision    <topic>-example-<n>-run-<k>, smallest k >= 2 that leaves
#                   every required HELPRUN-owned filename free
# Never overwrite, never rotate, no timestamps/UUIDs/hashes.
# ============================================================

_WINDOWS_INVALID_FILENAME_CHARS = '<>:"/\\|?*'

_WINDOWS_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}

SAFE_BASENAME_MAX_CHARS = 64


def safe_basename(topic):
    """Derive a Windows-safe, recognisable filename stem from a help topic.

    Only invalid characters are replaced and pathological length is bounded;
    the topic stays recognisable.  Length is bounded in characters, not bytes,
    because GATE 2 R17 showed strlen counts UTF-8 bytes while ustrlen counts
    characters -- truncating on bytes could split a character in half.
    """
    text = "" if topic is None else str(topic).strip()

    out = []
    for ch in text:
        if ch in _WINDOWS_INVALID_FILENAME_CHARS or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)

    cleaned = "".join(out).strip().rstrip(".")

    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")

    if len(cleaned) > SAFE_BASENAME_MAX_CHARS:
        cleaned = cleaned[:SAFE_BASENAME_MAX_CHARS].rstrip("._ ")

    if not cleaned:
        cleaned = "helprun-topic"

    if cleaned.split(".")[0].lower() in _WINDOWS_RESERVED_STEMS:
        cleaned = cleaned + "_"

    return cleaned


def _basename_candidates(topic, ordinal):
    stem = safe_basename(topic) + "-example-" + str(int(ordinal))

    yield stem

    k = 2
    while k <= 9999:
        yield stem + "-run-" + str(k)
        k += 1


def choose_run_basename(out_dir, topic, ordinal):
    """Smallest collision-free run basename, per the frozen section 16 policy.

    A basename is free only when no file in the output directory already starts
    with it, so the log and every graph/artifact of one click share one
    basename and none of them can overwrite an existing user file.
    """
    directory = Path(out_dir)

    try:
        existing = [p.name.lower() for p in directory.iterdir()]
    except OSError:
        existing = []

    for candidate in _basename_candidates(topic, ordinal):
        prefix = candidate.lower()

        # Any file already carrying this basename takes it, whatever suffix it
        # wears: the log, a graph, an exported artifact, the code record, the
        # standalone do-file or the manifest. Listing the suffixes one by one
        # meant each new kind of run file had to remember to add itself, and a
        # kind that forgot could let a second run claim a basename already in
        # use and detach a file from the run that produced it.
        collides = any(
            name == prefix or name.startswith(prefix + ".")
            or name.startswith(prefix + "-")
            for name in existing
        )

        if not collides:
            return candidate

    raise HelprunError(
        "OUTPUT_ARTIFACT_MISSING",
        "helprun: could not find a collision-free output basename",
    )


def topic_output_directory(parent_pwd, topic):
    """The frozen section 12.1 output root: <click-time c(pwd)>\\<safe-root-topic>\\

    Every HELPRUN-owned artifact of one clicked run -- the persistent log, the
    captured graphs and the authored final artifacts -- lives in this one
    directory, so nothing is scattered across the working-directory root.

    The root help topic the user sees names the directory, even when the help
    delegates to another physical file. The name is passed through the same
    safe_basename() rule used for filenames, and the result is verified to stay
    directly beneath the frozen parent, so a topic can never traverse out of it
    or turn into an absolute path.
    """
    base = Path(parent_pwd) if parent_pwd else Path.cwd()
    safe = safe_basename(topic)

    candidate = base / safe

    # A topic-derived name must resolve to a direct child of the frozen root.
    try:
        if candidate.resolve().parent != base.resolve():
            raise HelprunError(
                "OUTPUT_DIRECTORY_NOT_WRITABLE",
                "helprun: the help topic does not yield a safe output "
                "directory beneath the working directory",
            )
    except OSError:
        pass

    return candidate


def ensure_topic_directory(parent_pwd, topic):
    """Create the topic output directory, or report why it is unusable."""
    directory = topic_output_directory(parent_pwd, topic)

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    if not output_directory_writable(directory):
        return None

    return directory


def output_directory_writable(out_dir):
    """Prove writability by actually writing, not by inspecting attributes."""
    directory = Path(out_dir)

    if not directory.is_dir():
        return False

    probe = directory / (".helprun_write_probe_" + str(os.getpid()))

    try:
        probe.write_text("probe", encoding="utf-8")
    except OSError:
        return False

    try:
        probe.unlink()
    except OSError:
        pass

    return True


# ============================================================
# Stata executable / roots
# ============================================================

def stata_exe():
    buf = ctypes.create_unicode_buffer(32768)
    kernel32.GetModuleFileNameW(None, buf, len(buf))
    return Path(buf.value)


def ado_roots(exe, stata_roots=None):
    roots = []

    if stata_roots:
        for raw in stata_roots:
            if not raw:
                continue

            p = Path(raw)

            if (
                p.exists()
                and
                p not in roots
            ):
                roots.append(p)

    # Authoritative adopath order (GATE 2 R06): BASE, SITE, ".", PERSONAL,
    # PLUS, OLDPLACE.  PERSONAL precedes PLUS; the previous order had the two
    # inverted, which would resolve a duplicated topic name to the wrong file
    # whenever this fallback is used.
    fallback = [
        exe.parent / "ado" / "base",
        exe.parent / "ado" / "site",
        Path.cwd(),
        Path.home() / "ado" / "personal",
        Path.home() / "ado" / "plus",
    ]

    for p in fallback:
        if (
            p.exists()
            and
            p not in roots
        ):
            roots.append(p)

    return roots


def _python_file_search(name, roots):
    """Offline fallback only.  Mirrors Stata's <root>/<first letter>/<name>
    layout, in the authoritative adopath order supplied by the caller."""
    stem = Path(name).stem
    first = stem[0].lower() if stem else ""

    for root in roots:
        for candidate in (
            Path(root) / name,
            Path(root) / first / name,
        ):
            if candidate.exists():
                return Path(os.path.normpath(str(candidate)))

    return None


def resolve_source_file(name, roots):
    """Resolve one help source file by name.

    Inside Stata the Stata-resolved path is authoritative and a Python guess
    may never override it (specification section 4).  The Python search is used
    only when running outside Stata.
    """
    if stata_available():
        return stata_findfile(name)

    return _python_file_search(name, roots)


def resolve_help_topic(topic, roots):
    topic = topic.strip()

    if not topic:
        return None

    for ext in (".sthlp", ".hlp"):
        found = resolve_source_file(topic + ext, roots)
        if found is not None:
            return found

    return None


# ============================================================
# SMCL parser
# ============================================================

def norm(s):
    return re.sub(r"\s+", " ", s.strip())



def resolve_help_include(name, parent, roots):
    """Resolve an `INCLUDE help NAME` target.

    GATE 2 R05 proved, from the installed corpus, that Stata resolves the
    target through its ordinary adopath + first-letter-subdirectory search
    keyed on the *target's* own name -- never relative to the including file's
    directory.  regress.sthlp lives in base/r/ yet pulls shortdes-coeflegend
    from base/s/, fvvarlist from base/f/ and vce_mi from base/v/.

    `parent` is retained in the signature for call-site compatibility and is
    deliberately not used for precedence; searching it first was the previous
    behaviour and could select a different file than Stata would.
    """
    raw = name.strip().strip('"').strip("'")

    if not raw:
        return None

    p0 = Path(raw)

    if p0.suffix.lower() in (".ihlp", ".sthlp", ".hlp"):
        names = [raw]
    else:
        names = [raw + ext for ext in (".ihlp", ".sthlp", ".hlp")]

    for candidate_name in names:
        found = resolve_source_file(candidate_name, roots)
        if found is not None:
            return found

    return None


def include_target(raw):
    s = raw.strip()

    m = re.match(
        r"^INCLUDE\s+help\s+([A-Za-z0-9_./\\-]+)\s*$",
        s,
        flags=re.IGNORECASE
    )

    if m:
        return m.group(1)

    m = re.match(
        r"^\{include\s+help\s+([A-Za-z0-9_./\\-]+)\}\s*$",
        s,
        flags=re.IGNORECASE
    )

    if m:
        return m.group(1)

    return None


def _build_source_graph(path, roots, graph, stack, depth, max_depth):
    path = Path(path)

    try:
        canonical = str(path.resolve()).lower()
    except OSError:
        canonical = str(path).lower()

    if canonical in stack:
        raise HelprunSourceError(
            "HELP_FILE_SYNTAX_ERROR",
            "helprun: recursive help include cycle detected at " + str(path),
        )

    if depth > max_depth:
        raise HelprunSourceError(
            "HELP_FILE_SYNTAX_ERROR",
            "helprun: help include depth limit exceeded at " + str(path),
        )

    raw_lines, digest, encoding, size = read_source_bytes(path)
    graph.add_file(path, digest, encoding, size)

    next_stack = stack + [canonical]

    for lineno, raw in enumerate(raw_lines, start=1):
        target = include_target(raw)

        if target is None:
            graph.lines.append(SourceLine(raw, str(path), lineno))
            continue

        inc = resolve_help_include(target, path.parent, roots)

        if inc is None:
            raise HelprunSourceError(
                "PACKAGE_FILE_MISSING",
                "helprun: included help fragment not found: " + target,
                detail=str(path) + ":" + str(lineno),
            )

        _build_source_graph(
            inc, roots, graph, next_stack, depth + 1, max_depth
        )


def build_source_graph(path, roots, max_depth=16):
    """Expand a help source and every file it includes into one SourceGraph.

    The graph records each contributing file's SHA-256 and decoded encoding, so
    click identity can be bound to the whole source graph rather than to the
    root file alone (specification section 3, cases U-S11 / U-S12).
    """
    graph = SourceGraph()
    _build_source_graph(path, roots, graph, [], 0, max_depth)
    return graph


def read_help_lines(path, roots, stack=None, depth=0, max_depth=16):
    """Flat expanded line list.  Each element is a SourceLine carrying the
    contributing file path and line number it came from."""
    return build_source_graph(path, roots, max_depth=max_depth).lines


# ============================================================
# Source-bound click identity
#
# Specification section 3: clicking a visible example must execute that exact
# parsed example, not "whatever later becomes example number N".  The identity
# therefore carries the root topic, the resolved root source, a deterministic
# aggregate hash over the ordered contributing source graph (which covers every
# included .ihlp and delegated file, not just the root), and the structural
# locator of the example.
#
# The token is base64url of a compressed JSON payload.  That alphabet is
# A-Z a-z 0-9 - _ only, so the token can never contain a brace, quote,
# backtick or dollar sign and cannot break the SMCL link that carries it or
# inject anything into the Stata command line (case U-P26).
# ============================================================

CLICK_IDENTITY_VERSION = 1


def build_click_identity(root_topic, root_path, graph, unit):
    return {
        "v": CLICK_IDENTITY_VERSION,
        "topic": root_topic,
        "root": str(root_path),
        "agg": graph.aggregate_hash,
        "n": len(graph.files),
        "ord": int(unit["ordinal"]),
        "start": int(unit["start"]),
        "end": int(unit["end"]),
    }


def encode_click_identity(identity):
    raw = json.dumps(
        identity, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")

    packed = zlib.compress(raw, 9)

    return base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


# ============================================================
# CLICK HANDLES
# ============================================================
#
# The Viewer's Run control used to carry the whole encoded identity inline, so
# clicking it echoed a 200-plus character token into Results. Stata echoes a
# clicked {stata ...} link verbatim, so that noise was unavoidable as long as
# the payload travelled in the command.
#
# The payload no longer travels in the command. It is written to a private
# registry file and the link carries a short CONTENT-ADDRESSED handle: the
# handle IS the hash of the payload. Nothing about the identity changed, only
# how it is transported, so every protection is preserved:
#
#   anti-tampering       editing the stored payload changes its hash, so it no
#                        longer matches the handle that names it and the click
#                        is refused. Inventing a handle finds no file.
#   exact-example binding the payload is byte-for-byte what was encoded before,
#                        including root, ordinal, start, end and the aggregate
#                        source hash.
#   stale-source/engine  verify_click_identity() still rebuilds the source graph
#                        and compares the aggregate hash, exactly as before.
#
# A handle is not a capability: it grants nothing that the payload it names did
# not already grant, and it cannot be steered onto a different example, because
# any edit that would do so breaks the hash.

CLICK_HANDLE_CHARS = 16

_CLICK_HANDLE_RE = re.compile(r"^[0-9a-f]{%d}$" % CLICK_HANDLE_CHARS)


def click_registry_dir():
    """Private per-user directory holding the click payloads."""
    base = Path(tempfile.gettempdir()) / "helprun_clicks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def store_click_identity(identity):
    """Write one identity payload and return the handle that names it.

    The handle is the leading hex of the payload's SHA-256, so the mapping is
    content-addressed: the name cannot be separated from what it names.
    """
    payload = encode_click_identity(identity)
    handle = hashlib.sha256(payload.encode("ascii")).hexdigest()[:CLICK_HANDLE_CHARS]

    target = click_registry_dir() / (handle + ".txt")

    try:
        target.write_text(payload, encoding="ascii")
    except OSError as exc:
        raise HelprunError(
            "HELPRUN_INTERNAL_ERROR",
            "helprun: could not record the click identity",
            detail=str(exc),
        )

    return handle


def load_click_identity(handle):
    """Recover the payload a handle names, proving it was not altered."""
    text = "".join(str(handle).split()).lower()

    if not _CLICK_HANDLE_RE.match(text or ""):
        raise HelprunError(
            "HELPRUN_INTERNAL_ERROR",
            "helprun: malformed internal click handle",
        )

    source = click_registry_dir() / (text + ".txt")

    if not source.is_file():
        raise HelprunError(
            "SOURCE_CHANGED",
            "helprun: this Run control belongs to a help view that is no "
            "longer available; reopen the help and run helprun again",
            detail=text,
        )

    payload = source.read_text(encoding="ascii").strip()

    # The handle is the payload's own hash. A payload edited in place no longer
    # hashes to the name it is filed under, and is refused rather than run.
    actual = hashlib.sha256(payload.encode("ascii")).hexdigest()[:CLICK_HANDLE_CHARS]

    if actual != text:
        raise HelprunError(
            "SOURCE_CHANGED",
            "helprun: this Run control no longer matches the example it was "
            "created for; reopen the help and run helprun again",
            detail=text,
        )

    return payload


def prune_click_registry(keep_hours=24):
    """Drop stale payloads so the registry cannot grow without bound."""
    cutoff = time.time() - keep_hours * 3600

    try:
        entries = list(click_registry_dir().glob("*.txt"))
    except OSError:
        return

    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def decode_click_identity(token):
    text = "".join(str(token).split())

    if not re.fullmatch(r"[A-Za-z0-9_\-]+", text or ""):
        raise HelprunError(
            "HELPRUN_INTERNAL_ERROR",
            "helprun: malformed internal click token",
        )

    padded = text + "=" * (-len(text) % 4)

    try:
        identity = json.loads(
            zlib.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8")
        )
    except Exception as exc:
        raise HelprunError(
            "HELPRUN_INTERNAL_ERROR",
            "helprun: internal click token could not be decoded",
            detail=str(exc),
        )

    if not isinstance(identity, dict) or identity.get("v") != CLICK_IDENTITY_VERSION:
        raise HelprunError(
            "HELPRUN_INTERNAL_ERROR",
            "helprun: internal click token version is not supported",
        )

    return identity


def verify_click_identity(identity, roots):
    """Re-read the source graph and prove it still matches the clicked example.

    Any change to any contributing file -- root, included .ihlp, or delegated
    source -- changes the aggregate hash and produces SOURCE_CHANGED, so a
    renumbered or edited block can never be executed in place of the one the
    user actually clicked.
    """
    root = Path(identity["root"])

    if not root.exists():
        raise HelprunError(
            "SOURCE_CHANGED",
            "helprun: the help source this example came from is no longer "
            "available; reopen the help and run helprun again",
            detail=str(root),
        )

    graph = build_source_graph(root, roots)

    if (
        graph.aggregate_hash != identity.get("agg")
        or len(graph.files) != identity.get("n")
    ):
        raise HelprunError(
            "SOURCE_CHANGED",
            "helprun: the help source changed after this clickable view was "
            "prepared; reopen the help and run helprun again",
            detail=str(root),
        )

    return graph


def hidden(raw):
    return bool(re.match(r"^\s*\{\*", raw))


TEXT_TAGS = [
    "cmd", "inp", "bf", "it", "ul",
    "res", "txt", "err", "hi"
]


# ============================================================
# SMCL character codes and native command links
#
# Authority: GATE 2 R02, from base/s/smcl.sthlp.
#
#   {stata args[:text]}    -- syntax 3 and 4.  Displays <text> as a link that
#                             executes the Stata command <args>.  Syntax 3 is
#                             treated as syntax 4 with text == args.  A command
#                             containing a colon must be enclosed in quotes.
#   {matacmd args[:text]}  -- same, but submitted to Mata.
#
#   {c S|} -> $     {c 'g} -> `     {c -(} -> {     {c )-} -> }
#   {c #} / {c 0x##} -> the Latin1 character with that code, 1..255
#
# The runnable content is args, never the display text.
# ============================================================

SMCL_C_NAMED = {
    "S|": "$",
    "'g": "`",
    "-(": "{",
    ")-": "}",
}


def _smcl_c_value(arg):
    a = arg.strip()

    if a in SMCL_C_NAMED:
        return SMCL_C_NAMED[a]

    if re.fullmatch(r"0[xX][0-9A-Fa-f]{1,2}", a):
        n = int(a[2:], 16)
        if 1 <= n <= 255:
            return bytes([n]).decode("latin-1")
        return None

    if re.fullmatch(r"[0-9]{1,3}", a):
        n = int(a)
        if 1 <= n <= 255:
            return bytes([n]).decode("latin-1")

    return None


def decode_smcl_chars(s):
    """Replace {c ...} character codes with the characters they denote."""

    def rep(m):
        value = _smcl_c_value(m.group(1))
        return m.group(0) if value is None else value

    return re.sub(r"\{c\s+([^{}]*)\}", rep, s)


_SMCL_C_RE = re.compile(r"\{c\s+([^{}]*)\}")

_PROTECT_OPEN = "\x01"
_PROTECT_CLOSE = "\x02"
_PROTECT_RE = re.compile(_PROTECT_OPEN + r"(\d+)" + _PROTECT_CLOSE)


def protect_smcl_chars(s):
    """Replace {c ...} codes with brace-free sentinels. Returns (text, values).

    The codes have to be resolved before text-tag rendering, not after. A marked
    command legitimately contains one -- base/f/foreach.sthlp writes
    `{cmd:foreach x in a b c {c -(}}` -- and the text-tag patterns match
    `[^{}]*`, so they cannot span an encoded brace. The `{cmd:...}` marker then
    survived into the reconstructed command, which would reach Stata as
    `{cmd:foreach ...}` and fail r(199).

    Decoding the codes to real braces this early is not the answer either: that
    is exactly what the decode-last ordering was protecting against, since a
    literal brace would then be indistinguishable from an SMCL directive. A
    sentinel carries the value through the rendering passes untouched and
    contains no brace, so both properties hold at once.
    """
    values = []

    def rep(m):
        value = _smcl_c_value(m.group(1))
        if value is None:
            return m.group(0)
        values.append(value)
        return _PROTECT_OPEN + str(len(values) - 1) + _PROTECT_CLOSE

    return _SMCL_C_RE.sub(rep, s), values


def restore_protected_chars(s, values):
    """Put the protected {c ...} characters back."""
    if not values:
        return s

    def rep(m):
        index = int(m.group(1))
        return values[index] if index < len(values) else m.group(0)

    return _PROTECT_RE.sub(rep, s)


def _matching_brace(s, start):
    """s[start] must be '{'.  Return the index of its matching '}', else -1.

    Counts nested SMCL directives, so a {c -(} inside a {stata ...} target does
    not terminate the enclosing directive early.
    """
    depth = 0

    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i

    return -1


def _split_link_body(body):
    """Split a link body into (args, display_text) on the first colon that is
    at brace depth 0 and outside ALL quoting.  display_text is None for
    syntax 3.

    Quoting follows Stata's own rules, because that is what the Viewer applies
    when it runs the link: compound quotes `"..."' nest, and inside them a
    simple " is literal text rather than a quote boundary. The earlier version
    toggled one quote state on every ", so in `"... title("x") note("Data
    source: auto.dta")"' the inner "..." pairs flipped the state back to
    unquoted and the colon in "Data source:" was taken for the separator; the
    command came back cut short with a dangling opener (HPROD-41, seen on real
    pages). Quoting the command is precisely how an author protects an inner
    colon, so the colon that separates is the first one outside every quote.
    This is a rule about SMCL and Stata quoting; no topic takes part.
    """
    depth = 0
    compound = 0
    in_quote = False
    i = 0
    n = len(body)

    while i < n:
        ch = body[i]

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            if body.startswith('`"', i):
                compound += 1
                i += 2
                continue
            if compound and body.startswith('"\'', i):
                compound -= 1
                i += 2
                continue
            if ch == '"' and compound == 0:
                in_quote = not in_quote
            elif ch == ":" and compound == 0 and not in_quote:
                return body[:i], body[i + 1:]

        i += 1

    return body, None


def _unwrap_link_arg(arg):
    """Remove one enclosing quote layer, which exists only to protect a colon.

    Compound quotes are checked first so `"cmd"' does not lose only its outer
    backtick-quote pair asymmetrically.
    """
    a = arg.strip()

    if a.startswith('`"') and a.endswith('"\'') and len(a) >= 4:
        return a[2:-2]

    if len(a) >= 2 and a[0] == '"' and a[-1] == '"':
        return a[1:-1]

    return a


_LINK_NAMES = ("stata", "matacmd")


def scan_stata_links(s):
    """Scan one raw help line for native command links.

    Returns (text_with_each_link_replaced_by_its_command, [commands]).
    Authored command text is preserved exactly, including compound quotes and
    local/global macro syntax; only the enclosing SMCL directive and the one
    protective quote layer are removed.
    """
    out = []
    commands = []
    i = 0
    n = len(s)
    low = s.lower()

    while i < n:
        if s[i] == "{":
            name = None

            for candidate in _LINK_NAMES:
                if low.startswith("{" + candidate, i):
                    after = i + 1 + len(candidate)
                    if after < n and (s[after].isspace() or s[after] == "}"):
                        name = candidate
                        break

            if name is not None:
                close = _matching_brace(s, i)

                if close > i:
                    body = s[i + 1 + len(name):close]
                    args, _text = _split_link_body(body.strip())
                    command = decode_smcl_chars(_unwrap_link_arg(args)).strip()

                    out.append(command)

                    if command:
                        commands.append(command)

                    i = close + 1
                    continue

        out.append(s[i])
        i += 1

    return "".join(out), commands


def substitute_stata_links(s):
    return scan_stata_links(s)[0]


_PARAGRAPH_TAG_RE = re.compile(
    r"\{(?:pstd|phang\d?|pmore\d?|pin|p_end|p\s+[^}]*|break|\.\.\.)\}",
    flags=re.IGNORECASE,
)

_PARAGRAPH_START_RE = re.compile(
    r"^\s*\{(?:pstd|phang\d?|pmore\d?|pin|p\s+[^}]*)\}",
    flags=re.IGNORECASE,
)

_MARKED_SPAN_NAMES = ("cmd", "inp")

# Inline formatting and character-code directives that may appear inside a
# command line without making it structural.
_INLINE_SPAN_NAMES = frozenset({
    "cmd", "inp", "it", "bf", "hi", "res", "txt", "err", "ul", "c",
})


def next_paragraph_start(raw, current=True):
    """Does the line AFTER `raw` begin a paragraph body?

    Blank lines and {p_end} close a paragraph, so what follows starts fresh. A
    paragraph directive with prose text after it means the paragraph body has
    already begun on this line, so the next line is a continuation. A directive
    alone on its line means the body begins on the next line.
    """
    s = str(raw)

    if not s.strip():
        return True

    if re.search(r"\{p_end\}", s, flags=re.IGNORECASE):
        return True

    if _PARAGRAPH_START_RE.match(s):
        return _PARAGRAPH_TAG_RE.sub("", s).strip() == ""

    return False


# Closed-class English words that Stata never uses as bare command syntax.
# Stata's own bare keywords -- if, in, by, bysort, using, to, on, at, over,
# from, for, all, not, and, or, with, into, do, more -- are deliberately
# EXCLUDED, so a real command can never be read as prose because of them.
_PROSE_FUNCTION_WORDS = frozenset("""
    the this that these those it its their your our my his her
    is are was were been being
    has have had having
    will would shall should can could may might must
    of about above below between through during
    before after while because although though unless whether
    who whom whose which what when where why how
    than then however therefore thus also
    each every both either neither none other another
    such same many much few several
    he she we they you them
""".split())


def prose_continuation(text):
    """Does the plain text following a marked command span read as English?

    SMCL authors open a paragraph with the command's own name in a {cmd:...}
    span in two structurally identical ways:

        {pstd}{cmd:mycmd} price mpg, robust         <- a command with arguments
        {pstd}{cmd:mycmd} reports the mean of ...   <- an English sentence

    Both are "a marked span at the start of a paragraph body followed by plain
    text", so the opening marker cannot separate them. The general difference
    is the CONTINUATION: Stata syntax is varlists, numbers, operators and
    options, while a sentence is held together by closed-class English words.

    Quoted strings are removed first. A title("Mean of the outcome") may
    legitimately contain any English at all, because it is data, not syntax.
    """
    if not text.strip():
        return False

    bare = re.sub(r'"[^"]*"', " ", text)
    words = re.findall(r"[A-Za-z]+", bare.lower())

    if not words:
        return False

    hits = {w for w in words if w in _PROSE_FUNCTION_WORDS}

    # Two distinct function words is already decisive. One is decisive only
    # when the text also closes like a sentence, so that a real argument such
    # as `[your working directory]` stays a command.
    if len(hits) >= 2:
        return True

    return bool(hits) and len(words) >= 3 and bare.rstrip().endswith((".", "?", "!"))


def paragraph_visible_text(lines, index):
    """Visible text of the SMCL paragraph beginning at `index`.

    A paragraph is what an author writes as one sentence; the line breaks
    inside it are wrapping, not structure. It ends at {p_end}, at a blank
    line, or at the next paragraph or block directive.

    This exists because judging a paragraph by its first physical line is what
    manufactured a command out of a sentence. The real page wrote:

        {pstd}
        {cmd:neststatus, detail} reports 22 observations removed, nine variables
        removed, and the checkpoint to be restored. {cmd:nestrestore} then ...

    The first line alone carries one function word and no terminal stop, so it
    read as a command continuation and was joined; the words that make it
    unmistakably English -- "and", "to", "the", and the full stop -- were all
    on the following line. Stata then rejected `invalid 'nine'`. Sentences are
    not decided one line at a time.
    """
    out = []
    for raw in lines[index:]:
        stripped = raw.strip()
        if not stripped:
            break
        out.append(render(raw))
        if "{p_end}" in stripped:
            break
        if out and len(out) > 1 and re.match(
                r"^\{(?:p |pstd|phang|pmore|title|p2col|synopt|hline|marker)",
                stripped):
            out.pop()
            break
    return " ".join(part for part in out if part)


def line_is_marked_command(raw, at_paragraph_start=True, following_text=""):
    """Is this whole line an explicit {cmd:...} / {inp:...} command line?

    SMCL's {cmd} and {inp} directives are the author's own statement that the
    enclosed text is a command or input. When a line consists solely of such
    spans inside paragraph tags, its indentation carries no information, so the
    indentation heuristics used to spot plain-text code must not gate it. Real
    help files routinely write `{phang2}{cmd:. somecommand}{p_end}` at column 0.

    Requiring the ENTIRE line to be marked spans keeps ordinary prose that
    merely mentions {cmd:regress} in a sentence from being treated as code.

    The span scan is brace-aware rather than regex-based, because a marked
    command legitimately contains nested SMCL directives: base/f/foreach.sthlp
    writes `{cmd:foreach x in a b c {c -(}}`, where a `[^{}]*` pattern stops at
    the nested `{c -(}` and the whole line is missed.
    """
    if hidden(raw):
        return False

    # The line must begin a paragraph body. Prose wraps, and a wrapped
    # continuation can itself begin with a {cmd:...} span -- base/t/total.sthlp
    # writes
    #     {pstd}Estimate totals over values of {cmd:sex}, using {cmd:swgt} as
    #     {cmd:pweight}s{p_end}
    # where the second line is prose, not a command. Reconstructing it as one
    # produced the command `pweights` and an r(199).
    #
    # A line qualifies when it carries the paragraph directive itself, or when
    # the preceding context left us at the start of a paragraph body -- real
    # help also puts the directive on its own line:
    #     {p 4 8 2}
    #     {cmd:. cd [your working directory]}
    #     {p_end}
    if not (
        _PARAGRAPH_START_RE.match(str(raw))
        or at_paragraph_start
    ):
        return False

    stripped = _PARAGRAPH_TAG_RE.sub("", str(raw)).strip()

    if not stripped:
        return False

    length = len(stripped)
    low = stripped.lower()

    def span_name_at(index):
        if index >= length or stripped[index] != "{":
            return None
        m = re.match(r"\{([A-Za-z_][A-Za-z0-9_]*)", stripped[index:])
        return m.group(1).lower() if m else None

    # The line must OPEN with the author's own command marker. That is the
    # safety property: prose merely mentioning {cmd:regress} mid-sentence
    # begins with words, not with the marker, so it is never treated as code.
    opening = span_name_at(0)

    if opening not in _MARKED_SPAN_NAMES:
        return False

    # The rest of the command may be plain text interleaved with inline
    # formatting spans. Real help writes commands this way constantly:
    #   {pstd}{cmd:sctorezone} -3, only(starttime) force
    #   {pstd} {inp:ieddtab} {it:varlist} , {inp:t(}{it:time}{inp:)}
    # Any other directive means the line is structural, not a command.
    opening_close = _matching_brace(stripped, 0)

    if opening_close < 0:
        return False

    index = 0
    plain = []

    while index < length:
        if stripped[index] != "{":
            if index > opening_close:
                plain.append(stripped[index])
            index += 1
            continue

        name = span_name_at(index)

        if name not in _INLINE_SPAN_NAMES:
            return False

        close = _matching_brace(stripped, index)

        if close < 0:
            return False

        index = close + 1

    # The author's marker says the OPENING token is a command name. It does not
    # say the rest of the paragraph is code, and real help constantly opens a
    # descriptive sentence with the command's own name in a {cmd:...} span.
    #
    # The whole paragraph is judged, not this line alone: wrapping decides
    # where a sentence breaks, and a sentence broken mid-clause looks exactly
    # like a command continuation until its next line arrives.
    if prose_continuation("".join(plain) + " " + (following_text or "")):
        return False

    return True


# The authoritative Viewer stops parsing SMCL on a source line longer than this,
# and emits the raw text instead. Measured on StataNow 19.5 by opening a graded
# .sthlp with `help` and reading the switchover: LEN246 is the last line rendered
# as a clickable link, LEN247 and every longer line are shown literally. See
# validation/gate2_reference_addendum_R20A_R20E.md (R20A).
#
# The limit belongs to the help/.sthlp rendering path specifically. `view` on a
# .smcl does NOT impose it -- a graded probe there rendered 235..260 as links --
# so it must never be re-derived from `view`, and the constant may only be
# changed by re-measuring in the Viewer.
VIEWER_MAX_SMCL_LINE = 246


def viewer_parses_line(raw):
    """Would the authoritative Viewer parse this source line as SMCL at all?

    A line the Viewer refuses is shown to the reader as literal text, so it
    offers no clickable link. Anything helprun recovered from it would be an
    executable the user can never reach, which section 5 forbids inventing.
    """
    return len(str(raw).rstrip("\r\n")) <= VIEWER_MAX_SMCL_LINE


def native_link_commands(raw):
    """Commands contributed by native {stata ...} / {matacmd ...} links.

    A native link is a runnable command by the documented SMCL definition
    (GATE 2 R02): the author already declared it executable. Its indentation
    therefore carries no information, and the indentation heuristics used to
    recognise plain-text code must not be applied to it.
    """
    if hidden(raw):
        return []

    # R20A. A native link is only authored-runnable if the Viewer actually
    # presents it. Beyond the Viewer's line limit the directive is displayed
    # literally, so there is no link for the reader to click and helprun must
    # not manufacture one. This is a general length rule, not a rule about any
    # package: the installed reg2docx.sthlp is where it was found, but nothing
    # here refers to it.
    if not viewer_parses_line(raw):
        return []

    return scan_stata_links(raw)[1]


def render(raw):
    if hidden(raw):
        return ""

    s = raw

    # Native Stata command links.  For helprun execution semantics the runnable
    # content is the link TARGET, not the display label.  Resolved before
    # generic SMCL text-tag rendering, and with a brace-aware scanner so that
    # compound-quoted targets and {c -(} / {c )-} brace encodings survive.
    s = substitute_stata_links(s)

    # Brace encodings are set aside before any text tag is rendered, and put
    # back afterwards. See protect_smcl_chars: a {cmd:...} span may legitimately
    # contain one, and the tag patterns cannot match across a brace.
    s, protected_chars = protect_smcl_chars(s)

    # HPROD-04 again. The text-tag unwrap pattern is `[^{}]*`, so it cannot
    # unwrap a span that still contains a nested directive, and the purely
    # positional directives below were stripped only AFTER that loop. A span
    # such as
    #     {inp:idvars(make) version(15) {space 15}///}
    # was therefore never unwrapped, and the literal `{inp:` survived into the
    # reconstructed command. These directives carry no text of their own, so
    # removing them first is safe and lets the unwrap proceed.
    for _nested in (
        r"\{space\s+[^}]*\}",
        r"\{col\s+[^}]*\}",
        r"\{break\}",
    ):
        s = re.sub(_nested, "", s, flags=re.IGNORECASE)

    changed = True

    while changed:
        old = s

        for tag in TEXT_TAGS:
            s = re.sub(
                r"\{"
                +
                tag
                +
                r"\\?:([^{}]*)\}",
                r"\1",
                s,
                flags=re.IGNORECASE
            )

        changed = (
            old != s
        )

    for pattern in [
        r"\{pstd\}",
        r"\{phang\}",
        r"\{phang2\}",
        r"\{pmore\}",
        r"\{p\s+[^}]+\}",
        r"\{p_end\}",
        r"\{hline(?:\s+[^}]*)?\}",
        r"\{col\s+[^}]+\}",
        r"\{space\s+[^}]+\}",
        r"\{p2colset[^}]*\}",
        r"\{p2colreset\}",
        r"\{p2col[^}]*\}",
        r"\{synopt[^}]*\}",
        r"\{synopthdr[^}]*\}",
        r"\{synoptline\}",
        r"\{marker[^}]*\}",
        r"\{break\}"
    ]:
        s = re.sub(
            pattern,
            "",
            s,
            flags=re.IGNORECASE
        )

    # The protected codes come back only now, after every directive pattern has
    # run, so their literal braces were never visible to one. Any code that
    # protection did not recognise is decoded here as before.
    s = restore_protected_chars(s, protected_chars)
    s = decode_smcl_chars(s)

    return norm(
        s.replace(
            "{...}",
            ""
        )
    )


def extract_title(raw):
    m = re.match(
        r"^\s*\{title\\?:([^}]*)\}",
        raw,
        flags=re.IGNORECASE
    )
    if not m:
        return None
    return norm(m.group(1))


_SECTION_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*[.)]?\s*")

EXAMPLES_CONTAINER_EXACT = {
    "example",
    "examples",
    "remarks and examples",
    "remarks & examples",
    "remarks/examples",
}

# A section title that OPENS with one of these names the kind of section it is,
# so a later "examples" in it belongs to the subject rather than to the section.
# base/d/duplicates.sthlp titles a section
#
#     {title:Options for duplicates examples and duplicates list}
#
# where "examples" is part of the subcommand name `duplicates examples`. Reading
# that as an Examples section offered the option syntax as a runnable example.
#
# The list is deliberately short and leading-position only: "Remarks and
# examples" is a genuine container and must keep working, so a title is excluded
# only when its FIRST word already declares a different section kind.
_NON_EXAMPLE_TITLE_LEAD_RE = re.compile(
    r"^(?:options?|syntax|stored\s+results|also\s+see|acknowledge?ments?|"
    r"references?|authors?)\b",
    flags=re.IGNORECASE,
)


def examples_container(raw):
    """Is this {title:...} a section that CONTAINS examples?

    Real installed help uses many section-title forms for this, and a fixed
    list of four missed several of them:

        {title:Remarks/Examples}                    base/i/import_excel.sthlp
        {title:4. Examples}                         plus/d/domin.sthlp
        {title:Details and Examples : Sub-commands} plus/f/flexmat.sthlp

    So a leading section number is stripped and any title naming examples
    counts. A single-example heading such as "Example 1. ..." or
    "Examples: linear regression" is NOT a container -- titled_example handles
    those and keeps precedence, so per-example headings still define their own
    units.

    Over-recognising a container is bounded: a container only becomes a
    structural boundary when the region it opens actually carries runnable code
    and no sub-heading already claims it.
    """
    t = extract_title(raw)

    if t is None:
        return False

    if titled_example(raw):
        return False

    low = _SECTION_NUMBER_RE.sub("", t.strip().lower())

    if low in EXAMPLES_CONTAINER_EXACT:
        return True

    if _NON_EXAMPLE_TITLE_LEAD_RE.match(low):
        return False

    return bool(re.search(r"\bexamples?\b", low))


def titled_example(raw):
    t = extract_title(raw)

    if t is None:
        return False

    return bool(
        re.match(
            r"^example\s+[0-9]+\b",
            t,
            flags=re.IGNORECASE
        )
        or
        re.match(
            r"^example\s*:",
            t,
            flags=re.IGNORECASE
        )
        or
        re.match(
            r"^examples\s*:",
            t,
            flags=re.IGNORECASE
        )
        or
        # Nonnumeric example headings such as "Example. Rolling back a state"
        # or "Examples -- linear regression".  The separator is what makes it
        # a heading for one example rather than a section title like
        # "Examples and remarks".
        re.match(
            r"^examples?\s*[.–—-]\s*\S",
            t,
            flags=re.IGNORECASE
        )
    )


def visible_example_heading(raw):
    s = render(raw)

    if not s:
        return None

    if re.match(
        r"^Example\s+[0-9]+(?:\s*$|\s*[:.\-–—])",
        s,
        flags=re.IGNORECASE
    ):
        return s

    if re.match(
        r"^Example\s*:",
        s,
        flags=re.IGNORECASE
    ):
        return s

    return None


def has_examples(path, roots):
    for raw in read_help_lines(path, roots):
        if (
            examples_container(raw)
            or titled_example(raw)
            or visible_example_heading(raw) is not None
        ):
            return True
    return False


def help_links(path, roots):
    rows = []

    for line_no, raw in enumerate(
        read_help_lines(path, roots),
        start=1
    ):
        for m in re.finditer(
            r'"help\s+([A-Za-z0-9_]+)(?:##[A-Za-z0-9_]+)?"',
            raw,
            flags=re.IGNORECASE,
        ):
            rows.append((line_no, m.group(1), raw))

        for m in re.finditer(
            r"\{helpb?\s+([A-Za-z0-9_]+)",
            raw,
            flags=re.IGNORECASE,
        ):
            rows.append((line_no, m.group(1), raw))

    out = []
    seen = set()

    for row in rows:
        key = (row[0], row[1].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)

    return out


def locate_example_doc(
    root_topic,
    root_source,
    roots,
    max_depth=3
):
    queue = [
        (
            root_topic,
            root_source,
            0,
            [root_topic]
        )
    ]

    visited = set()

    while queue:
        topic, source, depth, chain = queue.pop(0)

        key = str(source).lower()
        if key in visited:
            continue
        visited.add(key)

        if has_examples(source, roots):
            return {
                "topic": topic,
                "source": source,
                "chain": chain,
                "depth": depth,
            }

        if depth >= max_depth:
            continue

        ranked = []

        for line_no, linked, raw in help_links(source, roots):
            score = 0
            low = raw.lower()

            if "example" in low:
                score += 100
            if "remark" in low:
                score += 50

            ranked.append(
                (-score, line_no, linked)
            )

        ranked.sort()

        for _, _, linked in ranked:
            source2 = resolve_help_topic(
                linked,
                roots
            )

            if source2 is None:
                continue

            queue.append(
                (
                    linked,
                    source2,
                    depth + 1,
                    chain + [linked]
                )
            )

    return None


def peer_structural_boundary(raw):
    """A {title:...} that is not itself an example heading ends the region."""
    t = extract_title(raw)

    return (
        t is not None
        and not examples_container(raw)
        and not titled_example(raw)
        and visible_example_heading(raw) is None
    )


# A dialog-tab sub-heading.  Stata renders {dlgtab:...} as a shaded, labelled
# full-width divider -- a structural directive the renderer draws, not prose
# that happens to look like a caption.  The optional {marker}/{...} prefix is
# how installed help usually anchors one.
_DIALOG_TAB_RE = re.compile(
    r"^\s*(?:\{marker\s+[^}]*\}\s*)?(?:\{\.\.\.\}\s*)?"
    r"\{dlgtab(?::\s*|\s+)(.*?)\s*\}\s*(?:\{\.\.\.\}\s*)?$",
    flags=re.IGNORECASE,
)


def dialog_tab_heading(raw):
    """The label of a {dlgtab:...} sub-heading line, or None.

    {dlgtab:...} is a strong structural boundary: unlike an ordinary {pstd}
    caption, which is just paragraph text an author may use for any purpose,
    a dialog tab is a directive the Viewer renders as a divider.  Inside an
    Examples region it therefore separates peer examples, and section 2's rule
    -- a unit runs to the next peer structural boundary -- applies to it.

    The rule is about SMCL structure only.  No help topic, package or command
    name takes part in the decision, and it deliberately does NOT extend to
    ordinary captions: promoting those would resegment roughly a thousand
    installed topics on evidence that does not distinguish a heading from a
    sentence.
    """
    m = _DIALOG_TAB_RE.match(raw or "")

    if m is None:
        return None

    label = (m.group(1) or "").strip()

    return label or None


def opens_example_region(raw):
    """Does this line open a region in which examples are expected?"""
    return examples_container(raw) or titled_example(raw)


def region_has_runnable_code(lines, start, end):
    """Does the 1-based inclusive line region contain any runnable command?

    Both frozen runnable forms count: a native {stata ...} command target, and
    ordinary/`{cmd:...}` command text that survives reconstruction.
    """
    para_start = True

    for n in range(start, min(end, len(lines)) + 1):
        raw = lines[n - 1]
        here = para_start
        para_start = next_paragraph_start(raw, para_start)

        if re.search(r"\{stata\b", raw, flags=re.IGNORECASE):
            return True

        if line_is_marked_command(raw, here):
            return True

        visible = render(raw)

        if not visible:
            continue

        if obvious_stata_output(raw, visible):
            continue

        stripped = strip_prompt(visible)

        if not stripped:
            continue

        if raw.lstrip().startswith(". ") or re.search(
            r"\{cmd\b", raw, flags=re.IGNORECASE
        ):
            return True

        if plausible_indented(raw, visible):
            return True

    return False


def extract_units(path, roots):
    lines = read_help_lines(path, roots)
    boundaries = []
    containers = []
    separators = []
    dialog_tabs = []
    inside = False
    # `inside` is sticky by design for the rules that follow it.  The dialog-tab
    # rule needs the narrower question -- am I still in the examples region the
    # container opened? -- because {dlgtab:...} is also the ordinary way to
    # subdivide an Options section, and those tabs are not examples.
    in_region = False

    for line_no, raw in enumerate(
        lines,
        start=1
    ):
        if examples_container(raw):
            inside = True
            in_region = True
            containers.append(
                (line_no, extract_title(raw))
            )
            continue

        if titled_example(raw):
            inside = True
            in_region = True
            boundaries.append(
                (line_no, extract_title(raw))
            )
            continue

        if peer_structural_boundary(raw):
            in_region = False

        if not inside:
            continue

        if in_region:
            tab = dialog_tab_heading(raw)

            if tab is not None:
                dialog_tabs.append((line_no, tab))
                continue

        heading = visible_example_heading(raw)
        if heading is not None:
            in_region = True
            boundaries.append(
                (line_no, heading)
            )
            continue

        # A deliberate alternative-branch separator ("Or:", "Alternatively:")
        # is a peer boundary: it ends one example and starts the alternative.
        # Splitting there is the opposite of guessing -- it presents both
        # alternatives as separate clickable examples instead of concatenating
        # them into one plan, which section 5 forbids. Without this, one
        # separator deep inside a large Examples section made the whole
        # section unrunnable, losing every unambiguous command in it.
        visible = render(raw)
        if visible and alternative_branch_marker(raw, visible):
            separators.append((line_no, visible.strip()))

    # A dialog-tab sub-heading inside an examples region becomes an example
    # boundary when the region it opens carries runnable code.  Requiring code
    # is what keeps the rule conservative: a prose-only tab is absorbed into the
    # example above it instead of manufacturing an empty clickable Example.
    #
    # This is promoted BEFORE the container loop below so that a container whose
    # examples are subdivided by dialog tabs sees itself as already claimed, and
    # nine separately headed examples are not offered as one undivided block.
    for i, (line_no, label) in enumerate(dialog_tabs):
        region_end = len(lines)

        if i + 1 < len(dialog_tabs):
            region_end = dialog_tabs[i + 1][0] - 1

        for n in range(line_no + 1, min(region_end, len(lines)) + 1):
            raw = lines[n - 1]

            if (
                peer_structural_boundary(raw)
                or opens_example_region(raw)
                or visible_example_heading(raw) is not None
            ):
                region_end = n - 1
                break

        if region_has_runnable_code(lines, line_no + 1, region_end):
            boundaries.append((line_no, label))

    # A container heading such as a singular {title:Example} is itself a
    # structural Example boundary when the region it opens carries runnable
    # code and no sub-heading inside that region already claims it.  This is
    # the cross-help structural rule of specification section 2: the unit runs
    # to the next peer structural boundary, and the native {stata ...} links
    # inside it stay one Example rather than becoming one Example per link.
    #
    # It is a general rule about SMCL structure.  No help topic, package or
    # command name takes part in the decision.
    for line_no, heading in containers:
        region_end = len(lines)

        for n in range(line_no + 1, len(lines) + 1):
            if peer_structural_boundary(lines[n - 1]):
                region_end = n - 1
                break

        claimed = any(
            line_no < b_line <= region_end
            for b_line, _ in boundaries
        )

        if claimed:
            continue

        if region_has_runnable_code(lines, line_no + 1, region_end):
            boundaries.append((line_no, heading))

    # Separators are merged only now, and were deliberately excluded from the
    # `claimed` test above: a separator inside a container must not stop the
    # container from opening its own first example, or the commands before the
    # separator would have no boundary and be lost.
    for line_no, text in separators:
        if any(b_line == line_no for b_line, _ in boundaries):
            continue
        if region_has_runnable_code(lines, line_no + 1, len(lines)):
            boundaries.append((line_no, text))

    boundaries.sort(key=lambda b: b[0])

    units = []

    for i, (line_no, heading) in enumerate(boundaries):
        start = line_no + 1

        # A unit ends at whichever comes first: the next example boundary, or
        # the next peer structural boundary (a {title:...} that is not an
        # example heading).  Using only the next example boundary would let a
        # trailing unit swallow Author/Also-see sections that follow it.
        end = len(lines)

        if i + 1 < len(boundaries):
            end = boundaries[i + 1][0] - 1

        for n in range(start, min(end, len(lines)) + 1):
            if peer_structural_boundary(lines[n - 1]):
                end = n - 1
                break

        units.append(
            {
                "ordinal": i + 1,
                "heading": heading,
                "start": start,
                "end": end,
            }
        )

    return units


def strip_prompt(s):
    s = s.strip()

    if s.startswith(". "):
        return s[2:].strip()

    if s.startswith("> "):
        return s[2:].strip()

    return s


def has_line_join(s):
    """Does this line carry a /// line-join marker?

    GATE 2 R08 observed that `///` joins the next physical line into the
    current logical line, and that this happens inside a `*` comment too: the
    comment then swallows the following line, which is therefore NOT a command.
    Stata requires `///` to be preceded by whitespace or start the line.
    """
    return bool(re.search(r"(?:^|\s)///", s))


def incomplete(s):
    t = s.rstrip()

    return (
        t.endswith(",")
        or t.endswith("///")
        or t.count("(") > t.count(")")
    )


def continuation(s):
    t = s.strip()

    if t.startswith("> "):
        return True

    t = strip_prompt(t)

    if t.startswith("("):
        return True

    return bool(
        re.match(
            r"^[A-Za-z_][A-Za-z0-9_]*\s*\(",
            t
        )
    )



def obvious_stata_output(raw, visible):
    s = visible.strip()

    if not s:
        return False

    # Text the author marked as DISPLAYED OUTPUT is output whatever word it
    # begins with. SMCL's {txt:...}, {res:...} and {err:...} (and the bare
    # style switches {txt}, {res}, {err}) are Stata's own output styles -- what
    # a log shows as text, result and error -- so a line whose paragraph
    # content opens with one of them renders what a command printed; it is
    # never a command. A help page that shows its expected output under the
    # command lines writes exactly this, and an output line that happens to
    # begin with the command word must not become a third command (HPROD-44).
    content = _PARAGRAPH_TAG_RE.sub("", str(raw)).strip()
    if re.match(r"^\{(?:txt|res|err|text|result|error)(?:[:}])", content, re.IGNORECASE):
        return True

    # Horizontal/table separators commonly emitted by Stata.
    if re.match(
        r"^[+\-_=|.\s]+$",
        s
    ):
        return True

    # Common result-table headers.
    if re.match(
        r"^(Variable|Source|Model|Residual|Total|Number of obs|F\(|Prob > F|R-squared|Adj R-squared|Root MSE)\b",
        s,
        flags=re.IGNORECASE
    ):
        return True

    # Table rows such as:
    #   price | 74 6165.257 ...
    #   Model | 8934540 ...
    #
    # Require a pipe plus a numeric/result-like right side so ordinary
    # Stata command text containing a pipe is not blanket-discarded.
    if "|" in s:
        # A vertical bar is also Stata's logical OR operator.  Do not
        # mistake ordinary commands such as
        #
        #     keep if x <= 10 | y >= 85
        #
        # for a rendered results-table row.
        if re.match(
            r"^(keep|drop|gen|generate|replace|assert|count)\b",
            s,
            flags=re.IGNORECASE
        ):
            return False

        left, right = s.split("|", 1)

        if (
            left.strip()
            and
            re.search(
                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
                right
            )
        ):
            return True

    return False


def numeric_paragraph_code_wrapper(raw):
    """
    Recognize generic numeric SMCL paragraph wrappers such as

        {p 4 4 2}command{p_end}
        {p 8 8 2}continuation{p_end}

    The numeric values are formatting parameters, not parser constants.
    """
    return bool(
        re.match(
            r"^\s*\{p\s+"
            r"\d+\s+\d+\s+\d+"
            r"(?:\s+[^}]*)?\}",
            raw,
            flags=re.IGNORECASE
        )
    )


def plausible_indented(raw, visible):
    # Real help files indent code with tabs as well as spaces -- base/f/
    # foreach.sthlp is tab-indented throughout. Counting only leading spaces
    # made every tab-indented example invisible, so tabs are expanded first.
    expanded = raw.expandtabs(8)
    leading = len(expanded) - len(expanded.lstrip(" "))
    numeric_p = numeric_paragraph_code_wrapper(raw)

    if leading < 8 and not numeric_p:
        return False

    if obvious_stata_output(raw, visible):
        return False

    low = visible.lower()

    if any(
        low.startswith(x)
        for x in [
            "note ",
            "where ",
            "this ",
            "these ",
            "the ",
            "we ",
            "you ",
            "for example",
            "for instance",
            "suppose ",
            "assume ",
        ]
    ):
        return False

    if visible.startswith("*") or visible.startswith("//"):
        return True

    # An English sentence is not a command, whatever paragraph wrapper it sits
    # in. Stata commands are lowercase, so a line that opens with an ordinary
    # capitalised word and closes with sentence punctuation is prose.
    #
    # This generalises the earlier guard below, which required >= 14 words and
    # no quotes and therefore let a short quoted instruction through as code --
    # a real page produced the reconstructed "command"
    #     Click the "Run this example" icon for Example 2.
    # Block bodies (program/input/mata/python) never reach this function, so a
    # legitimately capitalised Mata statement is unaffected.
    tokens = visible.split()
    if (
        len(tokens) >= 3
        and re.match(r"^[A-Z][a-z]+$", tokens[0])
        and visible.rstrip().endswith((".", "?", "!"))
    ):
        return False

    words = re.findall(r"[A-Za-z]+", visible)

    if (
        numeric_p
        and len(words) >= 14
        and visible.rstrip().endswith((".", "?", "!"))
        and '"' not in visible
        and "`" not in visible
    ):
        return False

    return True



def alternative_branch_marker(raw, visible):
    """Is this line a deliberate separator between two alternative code branches?

    Alternative branches must not be concatenated into one execution plan, so
    helprun refuses rather than guessing which branch the user meant. But the
    evidence has to be strong, which specification section 9 requires
    explicitly: "Do not guess."

    Matching any line that merely begins with "or" is not strong evidence. Help
    prose wraps, and a wrapped continuation line very often starts with "or" in
    ordinary English -- that alone disabled the Examples section of 36 real
    installed help files, base/a/anova.sthlp and base/f/foreach.sthlp among
    them. A genuine separator is a short standalone line introducing the
    alternative, and therefore ends with a colon.
    """
    s = visible.strip().lower()

    if len(s) > 48:
        return False

    return bool(
        re.match(
            r"^(or|alternatively|instead)\b[^.!?]*:$",
            s
        )
    )


# Stata's observation-number prompt inside an interactive `input` transcript.
_INPUT_OBS_PROMPT_RE = re.compile(r"^\d+\.\s+(.*)$")


_BREAK_TAIL_RE = re.compile(r"\{break\}\s*$", flags=re.IGNORECASE)
_P_END_RE = re.compile(r"\{p_end\}", flags=re.IGNORECASE)
_MARKED_FRAGMENT_RE = re.compile(r"^\{(?:cmd|inp):", flags=re.IGNORECASE)
# The `. ` prompt may be followed by a literal space, the end of the span, or
# another SMCL directive -- base/l/levelsof.sthlp writes `{cmd:.{space 8}di ...}`,
# where the prompt is present but spaced with {space 8} rather than a blank.
_MARKED_PROMPT_RE = re.compile(r"^\{(?:cmd|inp):\s*\.(?=$|\s|\}|\{)", flags=re.IGNORECASE)


# Characters that cannot begin a Stata command, so a line opening with one
# can only be the continuation of the line above.
_NOT_A_COMMAND_START_RE = re.compile(r"^[(\[,=|&<>+`]")


def break_continues_paragraph(raw):
    """Does this authored line continue its command on the next display line?

    GATE 2 R20B. SMCL authors write one long command across several physical
    lines by ending each but the last with `{break}`, closing the paragraph
    with `{p_end}` only on the last:

        {phang2}{cmd:. sem (Affective -> a1 a2 a3 a4 a5)}{break}
                {cmd:(Cognitive -> c1 c2 c3 c4 c5)}{p_end}

    `{break}` breaks the DISPLAY line. It does not close the paragraph and it
    is not a command terminator, so both lines are one authored command. The
    shape occurs 284 times across 83 unrelated installed help files, so this is
    a general SMCL rule and not a `sem` repair; see
    validation/gate2_reference_addendum_R20A_R20E.md.
    """
    s = str(raw)

    if _P_END_RE.search(s):
        return False

    return bool(_BREAK_TAIL_RE.search(s))


def marked_fragment_without_prompt(raw):
    """A {cmd:...} fragment that continues, rather than starts, a command.

    Two independent conditions must hold, and both are the author's own.

    First, GATE 2 R08 fixes the `. ` prompt as the marker that a new command
    begins: frames_intro.sthlp writes `{cmd:. frames dir}{break}` then
    `{cmd:. frame dir}`, which are two commands. A prompt therefore disqualifies
    the line immediately.

    Second, the absence of a prompt is NOT by itself evidence of continuation,
    because some authors use no prompts at all -- iesave.sthlp writes
    `{inp:sysuse auto, clear}{break}` then `{inp:local myfolder ...}`, which are
    separate commands. Measuring every `{break}`-continued fragment in both
    authoritative roots gives 39 prompted, 161 beginning with an ordinary word,
    and 91 beginning with a character that cannot start a Stata command at all
    (`(` 79, `[` 6, `,` 2, a backtick 2, `=` 2).

    Only that last group is joined. A fragment opening with `(`, `[`, `,`, `=`,
    an operator or a backtick cannot be a command, so treating it as the
    continuation of the line above is the only reading available -- no guess is
    involved. The 161 word-initial fragments stay ambiguous and are left alone,
    which is what R20B requires: reconstruct what a general rule justifies and
    refuse to guess the rest.
    """
    stripped = _PARAGRAPH_TAG_RE.sub("", str(raw)).strip()

    if not _MARKED_FRAGMENT_RE.match(stripped):
        return False

    if _MARKED_PROMPT_RE.match(stripped):
        return False

    visible = render(raw).strip()

    return bool(visible) and _NOT_A_COMMAND_START_RE.match(visible) is not None


class CommandList(list):
    """Reconstructed commands, plus whether they end inside an open block."""

    __slots__ = ("open_block", "last_command_line", "unreliable_fragments")

    def __init__(self, items=()):
        super().__init__(items)
        self.open_block = None
        # Source line of the final executable command, which is where the Run
        # control belongs. None means it could not be determined.
        self.last_command_line = None
        # Fragments that could not be read as a command and that no open
        # delimiter above them justified joining. Carried rather than executed:
        # reconstructing an authored continuation is permitted, inventing a
        # missing one is not.
        self.unreliable_fragments = []


_COMMAND_PREFIXES = frozenset({
    "quietly", "qui", "noisily", "noi", "capture", "cap", "by", "bysort",
    "sort", "version", "svy", "mi", "statsby", "bootstrap", "jackknife",
    "permute", "simulate", "nestled", "stepwise", "sw", "xi", "fvset",
})


# Characters that cannot begin a Stata command line. This is the codebase's
# own R20B measurement, not a fresh guess: across both authoritative roots, 91
# real continued fragments began with one of these, and no real command does.
# Exactly the measured set -- ( 79, [ 6, , 2, backtick 2, = 2 -- plus the
# double quote, which is the opener that produced the reported r(199). The
# comparison and logical operators were an addition of mine and immediately
# mis-flagged a Stata OUTPUT row, `| make mpg |`, inside a fixture whose whole
# purpose is output that must not be read as code. Widening a measured set by
# intuition is how a measurement stops meaning anything.
_FRAGMENT_OPENERS = ("(", "[", ",", "=", chr(96), chr(34))


def command_shaped(text):
    """Could this text begin a Stata command at all?

    Deliberately narrow. An earlier version also rejected an identifier
    followed immediately by an opening parenthesis, reasoning that `note(...)`
    is an option fragment. That was too clever: it flagged `#delimit ;`,
    `python:`, `input` data rows, comment lines and the closing brace of a
    block -- six real fixtures -- because plenty of legitimate Stata lines do
    not start with a bare command word.

    So the test is the one the codebase already established by measurement: a
    line opening with a character that cannot start a command is a
    continuation fragment. That covers both reported failures -- the compound
    quote opener that produced `is not a valid command name`, and the
    parenthesised SEM continuations -- without inventing a rule about what a
    command may look like.
    """
    s = str(text).strip()
    if not s:
        return False
    return not s.startswith(_FRAGMENT_OPENERS)


def _unbalanced(text):
    """Does this command leave a quote or parenthesis open?

    Only an unbalanced tail is evidence that the next line continues it. This
    is what separates reconstructing an authored continuation from inventing
    one: with an open delimiter the join is forced, without one it is a guess.
    """
    s = str(text)
    depth = 0
    in_dq = False
    i = 0
    compound = 0
    while i < len(s):
        two = s[i:i + 2]
        if two == chr(96) + chr(34):
            compound += 1
            i += 2
            continue
        if two == chr(34) + chr(39) and compound:
            compound -= 1
            i += 2
            continue
        ch = s[i]
        if ch == chr(34) and not compound:
            in_dq = not in_dq
        elif not in_dq and not compound:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        i += 1
    return depth > 0 or in_dq or compound > 0


UNRELIABLE_RECONSTRUCTION = "UNRELIABLE_RECONSTRUCTION"


def repair_fragments(commands):
    """Fold fragments into the command they continue, or report unreliability.

    Returns (commands, unreliable). A fragment is joined ONLY when the command
    above it leaves a delimiter open, which is evidence-backed continuation.
    Where no such evidence exists the sequence is reported unreliable rather
    than executed: the specification is explicit that helprun may reconstruct
    authored continuation and may not invent missing continuation, and running
    an illegal command in the hope that it works is exactly the invention it
    forbids.
    """
    out = []
    unreliable = []
    depth = 0

    for command in commands:
        text = str(command)

        # Inside a brace block -- while, forvalues, foreach, if/else, program,
        # Mata -- the body lines and the closing brace are legitimate content,
        # not fragments. A bare `}` is not command-shaped and must not be
        # mistaken for one: treating it as an unjustifiable fragment refused
        # every looping example on the page.
        if depth > 0:
            out.append(command)
            depth += text.count("{") - text.count("}")
            depth = max(0, depth)
            continue

        opens = text.count("{") - text.count("}")

        if command_shaped(command) or not out:
            if not command_shaped(command) and not out:
                unreliable.append(command)
            out.append(command)
            depth = max(0, depth + opens)
            continue

        if _unbalanced(out[-1]):
            out[-1] = out[-1].rstrip() + " " + text.strip()
            continue

        # A fragment with nothing open above it. Joining would be a guess and
        # emitting it alone is an illegal command, so neither is permitted.
        unreliable.append(command)
        out.append(command)
        depth = max(0, depth + opens)

    return out, unreliable


def reconstruct_unit(path, unit, roots, source_out=None):
    """The unit's runnable commands.

    `source_out`, when a list is passed, receives the AUTHOR'S OWN lines --
    every source line the reconstructor accepted as code, rendered, in the
    order the page presents them. The commands are what helprun executes; these
    are what the author wrote, and the two are different representations that
    must never be presented as one another.
    """
    lines = read_help_lines(path, roots)
    commands = []
    source_fragments = source_out if source_out is not None else []
    current = None
    block_mode = None
    comment_join_pending = False
    alternative_pending = False
    para_start = True
    break_pending = False

    # The Run control must be inserted immediately after the FINAL
    # EXECUTABLE COMMAND of the example, never at the unit's structural
    # end: a trailing non-runnable section -- a video list, a notes or
    # references block, a native-link collection, plain prose -- lies
    # inside the unit range but after the last thing that runs, and a
    # control placed beyond it appears to belong to that section instead.
    # None means no line contributed a command, which is a refusal, not a
    # default to the end of the range.
    last_command_line = None

    def flush_current():
        nonlocal current

        if current is not None:
            commands.append(current)
            current = None

    for line_no in range(
        unit["start"],
        unit["end"] + 1
    ):
        raw = lines[line_no - 1]
        here_para_start = para_start
        para_start = next_paragraph_start(raw, para_start)
        here_break = break_pending

        if hidden(raw):
            continue

        visible = render(raw)

        if not visible:
            continue

        break_pending = break_continues_paragraph(raw)

        stripped = strip_prompt(visible)
        low = stripped.lower()

        # ----------------------------------------------------
        # Alternative code branches
        #
        # A marker such as "Or", "Alternatively" or "Instead" only makes an
        # example ambiguous when it actually separates two alternative CODE
        # branches -- code before it and more code after it. Treating every
        # prose sentence that merely begins with one of those words as a branch
        # marker disabled the whole example, which is what happened to 47 real
        # help files including base/f/foreach.sthlp. Ordinary prose is skipped,
        # as prose always was.
        # ----------------------------------------------------
        link_commands = native_link_commands(raw)

        accepted_as_code = bool(
            link_commands
            or block_mode is not None
            or BLOCK_OPEN_RE.match(stripped)
            or visible.lstrip().startswith("> ")
            or visible.startswith(". ")
            or plausible_indented(raw, visible)
            or line_is_marked_command(raw, here_para_start, paragraph_visible_text(lines, line_no - 1))
        )

        if alternative_branch_marker(raw, visible) and not accepted_as_code:
            if commands or current is not None:
                alternative_pending = True
            continue

        if accepted_as_code and alternative_pending:
            raise HelprunError(
                "AMBIGUOUS_EXAMPLE_RECONSTRUCTION",
                "helprun: this example offers alternative branches, so which "
                "commands to run cannot be determined without guessing"
            )

        # The author's own line, exactly as it renders, recorded at the moment
        # the reconstructor accepts it as code. It is kept alongside the
        # commands, never merged into them: the code record has to show what
        # the author supplied separately from what helprun derived, and the two
        # differ wherever proven SMCL structure joined several source fragments
        # into one command. Recording it HERE, from the same test that accepts
        # the line, is what stops the two readings drifting apart.
        if accepted_as_code:
            source_fragments.append(visible.rstrip())

        # ----------------------------------------------------
        # Displayed continuation
        #
        # GATE 2 R08: in help text and logs alike, a leading ">" marks the
        # continuation of the command displayed on the previous line. It is a
        # display artifact, not a command of its own, so it must be joined
        # rather than executed separately.
        # ----------------------------------------------------
        if visible.lstrip().startswith("> ") and block_mode is None:
            if current is not None:
                current = norm(current + " " + stripped)
                last_command_line = line_no
                continue
            if commands and not comment_join_pending:
                commands[-1] = norm(commands[-1] + " " + stripped)
                continue

        # ----------------------------------------------------
        # Comment line-join absorption
        #
        # A `*` comment carrying a /// marker continues onto the next physical
        # line, which is then comment text and must never be executed -- not
        # even when it looks like a command or is a native {stata ...} link.
        # ----------------------------------------------------
        if comment_join_pending:
            if commands:
                commands[-1] = norm(commands[-1] + " " + stripped)

            comment_join_pending = has_line_join(visible)
            continue

        if (
            (stripped.startswith("*") or stripped.startswith("//"))
            and (visible.startswith(". ") or plausible_indented(raw, visible))
            and block_mode is None
        ):
            flush_current()
            commands.append(stripped)
            last_command_line = line_no
            comment_join_pending = has_line_join(visible)
            continue

        # ----------------------------------------------------
        # Native command links
        #
        # {stata ...} / {matacmd ...} targets are runnable by definition, so
        # they bypass the indentation heuristics that recognise plain-text
        # code.  Real help files place such links at column 0 under a bare
        # {phang}, which those heuristics would otherwise reject.
        #
        # Each link is one authored command and keeps its own line, so a link
        # that opens a brace block and the links that form its body remain
        # separate statements rather than being folded into one line.
        # ----------------------------------------------------
        # link_commands was computed above with accepted_as_code

        if link_commands:
            flush_current()

            for command in link_commands:
                commands.append(command)
                last_command_line = line_no
                cmd_low = command.lower()

                if block_mode is not None:
                    if cmd_low == "end":
                        block_mode = None
                    continue

                if re.match(r"^mata\s*:$", cmd_low):
                    block_mode = "mata"
                elif re.match(r"^python\s*:$", cmd_low):
                    block_mode = "python"
                elif re.match(r"^program\s+(define|def)\b", cmd_low):
                    block_mode = "program"
                elif re.match(r"^input(?:\s|$)", cmd_low):
                    block_mode = "input"

            continue

        # ----------------------------------------------------
        # Language / structural blocks
        #
        # Once inside one of these blocks, preserve authored
        # physical lines.  Do NOT apply ordinary continuation
        # heuristics such as "name(...)" because Mata/Python
        # function calls are independent statements.
        # ----------------------------------------------------
        if block_mode is not None:
            flush_current()

            payload = stripped

            if block_mode == "input":
                # An authored `input` example is usually a transcript of the
                # interactive session, and Stata echoes an observation-number
                # prompt before every line of it, the terminator included:
                #
                #     . input str15 number
                #                     number
                #          1. "(123) 456-7890"
                #          2. "(800) STATAPC"
                #          3. end
                #
                # That prompt is Stata's output, not authored data. Leaving it
                # in fed `1. "(123) 456-7890"` to input as if it were a value,
                # and worse, meant the `end` was never recognised, so the block
                # never closed. Seventeen real installed help files are written
                # this way, including base/i/input.sthlp, base/s/save.sthlp,
                # base/c/cross.sthlp and the nine f_regex* function files.
                prompt = _INPUT_OBS_PROMPT_RE.match(payload)
                if prompt:
                    payload = prompt.group(1).strip()

            if payload:
                commands.append(payload)
                last_command_line = line_no

            if payload.lower() == "end":
                block_mode = None

            continue

        is_mata_start = bool(
            re.match(
                r"^mata\s*:$",
                low
            )
        )

        is_python_start = bool(
            re.match(
                r"^python\s*:$",
                low
            )
        )

        is_program_start = bool(
            re.match(
                r"^program\s+(define|def)\b",
                low
            )
        )

        is_input_start = bool(
            re.match(
                r"^input(?:\s|$)",
                low
            )
        )

        if (
            is_mata_start
            or is_python_start
            or is_program_start
            or is_input_start
        ):
            flush_current()
            commands.append(stripped)
            last_command_line = line_no

            if is_mata_start:
                block_mode = "mata"

            elif is_python_start:
                block_mode = "python"

            elif is_program_start:
                block_mode = "program"

            else:
                block_mode = "input"

            continue

        if visible.startswith(". "):
            flush_current()
            current = stripped
            last_command_line = line_no
            continue

        if current is not None:
            # A Stata comment is a complete physical command line.
            # Never attach the next command to it merely because that
            # next command starts with name(...), for example collapse (...).
            if (
                current.lstrip().startswith("*")
                or current.lstrip().startswith("//")
            ):
                flush_current()

            elif incomplete(current):
                left = current.rstrip()

                if left.endswith("///"):
                    left = left[:-3].rstrip()

                current = (
                    left
                    + " "
                    + stripped
                )
                last_command_line = line_no
                continue

            elif here_break and marked_fragment_without_prompt(raw):
                # GATE 2 R20B: the previous authored line closed with {break}
                # inside a still-open paragraph and this line is the author's
                # own marked fragment carrying no `. ` prompt, so the two
                # display lines are one authored command.
                current = current.rstrip() + " " + stripped
                last_command_line = line_no
                continue

            else:
                flush_current()

        # GATE 2 R20B, complementary half. `{break}` is an explicit authored
        # display break, so the line after one begins a fresh display line and
        # may carry the author's own marked command. Without this the second
        # and later fragments of a {break}-broken paragraph are dropped
        # outright: iesave.sthlp writes four authored commands per Example and
        # only `sysuse auto, clear` survived, so a click would have run a
        # fraction of the Example and reported SUCCESS.
        #
        # Two guards keep the relaxation from reopening anything. The break
        # must be the author's own -- wrapped prose has none, it simply runs on
        # -- and no block may be open, because inside program/input/Mata bodies
        # the body-capture path already owns every line and must not be
        # second-guessed. prose_continuation() still guards the line itself.
        break_line_command = (
            here_break
            and block_mode is None
            and line_is_marked_command(raw, True, paragraph_visible_text(lines, line_no - 1))
        )

        if (
            plausible_indented(raw, visible)
            or line_is_marked_command(raw, here_para_start, paragraph_visible_text(lines, line_no - 1))
            or break_line_command
        ):
            if (
                visible_example_heading(raw)
                is not None
            ):
                continue

            current = stripped
            last_command_line = line_no

    flush_current()

    normalized = CommandList(
        norm(x)
        for x in commands
        if norm(x)
    )

    # A unit that ends with a block still open cannot be RUN: Stata would be fed
    # an unterminated block, and for `input` it would wait for data that never
    # arrives. Two quite different things produce that state, and only one of
    # them is a parser error:
    #
    #   * an alternative-branch separator inside a `program`, `input`, Mata or
    #     `#delimit ;` block splits it, leaving this unit holding the opener;
    #   * the author simply wrote the transcript without its terminator, which
    #     is ordinary Stata documentation style -- base/n/newvarlist.sthlp and
    #     base/i/input.sthlp both show `input` sessions with the data rows and
    #     no `end`.
    #
    # Either way the answer is to refuse the RUN, never to invent the missing
    # terminator, which section 5 forbids as silent semantic rewriting. But the
    # refusal belongs at click time, not here. Refusing during reconstruction
    # discards the whole unit, so the file reports no runnable example at all:
    # the user sees nothing and is told nothing, and the independent candidate
    # scan correctly flags the file as an unexplained zero-unit result, which is
    # a GATE 6 failure. Seventeen real installed help files were disabled that
    # way. The unit is therefore returned, carrying the open block, and
    # click_run refuses with a frozen reason if it is ever clicked.
    normalized.open_block = block_mode
    normalized.last_command_line = last_command_line

    # Fold continuation fragments into the command they continue, and record
    # any fragment that cannot be justified. An unreliable unit is still
    # RETURNED -- discarding it would hide the example from the Viewer and
    # report the file as having no runnable content, which is the failure mode
    # the open_block comment above describes -- and click_run refuses with the
    # frozen reason if it is ever clicked.
    repaired, unreliable = repair_fragments(list(normalized))
    if repaired != list(normalized):
        normalized[:] = repaired
    normalized.unreliable_fragments = unreliable

    # Preserve authored command order exactly.  Repeated commands are
    # semantically meaningful in many help examples (for example repeated
    # nestrestore calls) and must never be removed merely because their text
    # is identical.
    return normalized



# ============================================================
# FILE_PACKAGE_DEPENDENCY
# ============================================================

def _unquote_path(token):
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    return token


def _unsafe_external_path(path_text):
    s = _unquote_path(path_text).strip().replace("/", "\\")
    if not s:
        return False
    if s.startswith("\\\\"):
        return True
    if re.match(r"^[A-Za-z]:\\", s):
        return True
    parts = [p for p in s.split("\\") if p not in ("", ".")]
    return ".." in parts


def _extract_input_file_refs(command):
    s = strip_prefixes(command).strip()
    refs = []

    m = re.match(r'^(do|run)\s+(".*?"|\S+)', s, flags=re.IGNORECASE)
    if m:
        refs.append(("do", m.group(2)))
        return refs

    m = re.match(r'^use\s+(".*?"|\S+)', s, flags=re.IGNORECASE)
    if m:
        raw = _unquote_path(m.group(1))
        if re.search(r'\.(dta|dtas)$', raw, flags=re.IGNORECASE):
            refs.append(("data", m.group(1)))
        return refs

    # `using` names an OUTPUT for the writing commands, so it must not be
    # staged as a required input. Treating an authored output file as a
    # missing dependency made a perfectly good example fail before it ran.
    writes_using = re.match(
        r"^(export|outfile|outsheet|save|saveold|putexcel|putdocx|putpdf|"
        r"log|translate|graph\s+export|estout|esttab)\b",
        s,
        flags=re.IGNORECASE,
    )

    if not writes_using:
        m = re.search(r'\busing\s+(".*?"|\S+)', s, flags=re.IGNORECASE)
        if m:
            raw = _unquote_path(m.group(1))
            if re.search(
                r'\.(dta|csv|txt|raw|dat|do|ado|mata|mmat|json|xml)$',
                raw,
                flags=re.IGNORECASE
            ):
                refs.append(("using", m.group(1)))

    m = re.match(
        r'^import\s+(?:delimited|excel)\s+(".*?"|\S+)',
        s,
        flags=re.IGNORECASE
    )
    if m:
        refs.append(("import", m.group(1)))

    return refs


def _resolve_package_dependency(ref_token, source_dir, roots):
    raw = _unquote_path(ref_token).strip()

    if not raw:
        raise RuntimeError("helprun: empty package dependency path")

    if (
        "`" in raw
        or "${" in raw
        or re.search(r'\$[A-Za-z_][A-Za-z0-9_]*', raw)
    ):
        raise RuntimeError(
            "helprun: dynamic package dependency path cannot be resolved safely"
        )

    if _unsafe_external_path(raw):
        raise RuntimeError(
            "helprun: unsafe package dependency path: " + raw
        )

    rel = Path(raw.replace("\\", os.sep).replace("/", os.sep))
    candidates = [Path(source_dir) / rel]

    for root in roots:
        candidates.append(Path(root) / rel)

    exact = []
    seen = set()

    for p in candidates:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_file():
            exact.append(p)

    canonical = {}
    for p in exact:
        canonical[str(p.resolve()).lower()] = p
    exact = list(canonical.values())

    if len(exact) == 1:
        return exact[0]

    if len(exact) > 1:
        raise RuntimeError(
            "helprun: ambiguous package dependency: " + raw
        )

    if len(rel.parts) == 1:
        basename = rel.name
        search_dirs = [Path(source_dir)]
        first = basename[0].lower() if basename else ""

        for root in roots:
            root = Path(root)
            if first and (root / first).exists():
                search_dirs.append(root / first)

        hits = {}
        visited = set()

        for d in search_dirs:
            key = str(d).lower()
            if key in visited or not d.exists():
                continue
            visited.add(key)

            for p in d.rglob(basename):
                if p.is_file():
                    hits[str(p.resolve()).lower()] = p

        hits = list(hits.values())

        if len(hits) == 1:
            return hits[0]

        if len(hits) > 1:
            raise RuntimeError(
                "helprun: ambiguous package dependency: " + raw
            )

    raise RuntimeError(
        "helprun: unresolved package dependency: " + raw
    )


def _stage_package_dependencies(commands, source_dir, roots, sandbox):
    sandbox = Path(sandbox)

    # A file the example itself writes earlier in the same run is produced at
    # runtime, not shipped with the package, so it must not be staged as a
    # prerequisite input. Saving a dataset and reading it back across an
    # authored process boundary is the ordinary form of this.
    created_here = example_created_files(commands)

    for command in commands:
        for _, ref_token in _extract_input_file_refs(command):
            raw = _unquote_path(ref_token).strip()

            if (
                raw.lower() in created_here
                or (raw + ".dta").lower() in created_here
            ):
                continue

            resolved = _resolve_package_dependency(
                ref_token,
                source_dir,
                roots
            )

            rel = Path(raw.replace("\\", os.sep).replace("/", os.sep))
            dest_rel = rel if len(rel.parts) > 1 else Path(rel.name)
            dest = sandbox / dest_rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            if dest.exists():
                if dest.read_bytes() != resolved.read_bytes():
                    raise RuntimeError(
                        "helprun: staged package dependency collision: " + raw
                    )
            else:
                shutil.copy2(resolved, dest)


# ============================================================
# Guard
# ============================================================

def strip_prefixes(command):
    s = command.lstrip()
    changed = True

    while changed:
        changed = False

        m = re.match(
            r"^(quietly|qui|capture|cap|noisily|noi)\s+",
            s,
            flags=re.IGNORECASE,
        )

        if m:
            s = s[m.end():].lstrip()
            changed = True
            continue

        m = re.match(
            r"^(by|bysort)\b[^:]*:\s*",
            s,
            flags=re.IGNORECASE,
        )

        if m:
            s = s[m.end():].lstrip()
            changed = True

    return s


def command_token(command):
    s = strip_prefixes(command)

    if s.startswith("!"):
        return "!"

    m = re.match(
        r"^([A-Za-z_][A-Za-z0-9_]*)",
        s
    )

    if not m:
        return ""

    return m.group(1).lower()


def _quoted_paths(command):
    return re.findall(r'"([^"]+)"', command)


# Commands that require a human at a GUI. A hidden child Stata would block on
# them forever, so they are refused as USER_INTERACTION_REQUIRED rather than
# being allowed to hang. This is an interactivity check, not a policy
# blacklist: runtime and safety policy belongs to the guard, which decides by
# role, provenance and target rather than by command name or file extension.
INTERACTIVE_COMMANDS = {
    "edit", "doedit", "browse", "db", "dialog", "help", "view",
}


def interactive_command(command):
    token = command_token(command)
    return token in INTERACTIVE_COMMANDS


# ------------------------------------------------------------
# Interactive INPUT inside a program (HPROD-34 remainder, HPROD-42).
#
# helprun normally executes examples in a hidden BATCH child. A program that
# reads from the user -- `display _request(...)`, `pause`, `window stopbox`,
# `window dialog`, `window menu`, `db` -- can never receive that input there:
# measured, _request() returns in a millisecond with an empty value in batch
# mode, and a program that notices c(mode)=="batch" may decline its own
# operation while the child still exits cleanly, which helprun then reported
# as SUCCESS (the user's varorder report).
#
# The general rule, keyed on what the invoked program READS, never on which
# program it is: before execution the program each authored command resolves
# to (as Stata resolves it) is scanned for input primitives. An example that
# needs input runs in a VISIBLE interactive Stata that helprun launches and
# owns, titled so the user can find it; the user answers the example's own
# prompt there, by hand, once. helprun never types, approves, guesses, changes
# window focus, uses the clipboard or a hook, and never touches a Stata it did
# not launch. Stata Automation is not used (activation would be answered by
# the user's own running Stata: experiments X5-X7b), so no registration is
# needed and helprun never changes system registration.
# ------------------------------------------------------------

# Primitives that BLOCK the interpreter until the user acts. `window menu`,
# `window dialog` and `db` are GUI side effects that never wait -- sysuse.ado
# in BASE contains a `window menu` statement, and treating it as a requirement
# routed every `sysuse` example to the visible worker (HPROD-43). `pause`
# blocks only while pause is ON, so a program's `pause` counts only when the
# same program (or the authored example) switches it on.
_INTERACTIVE_INPUT_RES = (
    ("_request()", re.compile(r"\b_request\s*\(", re.IGNORECASE)),
    ("window stopbox", re.compile(r"\bwindow\s+stopbox\b", re.IGNORECASE)),
)
_PAUSE_CMD_RE = re.compile(
    r"^\s*(?:capture\s+|noisily\s+|quietly\s+)*pause\b(?!\s+(?:on|off)\s*$)",
    re.IGNORECASE | re.MULTILINE)
_PAUSE_ON_RE = re.compile(r"^\s*(?:capture\s+|noisily\s+|quietly\s+)*pause\s+on\s*$",
                          re.IGNORECASE | re.MULTILINE)

_STRING_LITERAL_RE = re.compile(r'`"(?:[^"]|"(?!\x27))*"\x27|"[^"\n]*"')


def _program_code_only(text):
    """Strip comments and string literals so prose cannot trip the scan."""
    out = []
    for raw in text.splitlines():
        s = raw
        if s.lstrip().startswith("*"):
            continue
        s = re.sub(r"//.*$", "", s)
        s = _STRING_LITERAL_RE.sub('""', s)
        out.append(s)
    joined = "\n".join(out)
    joined = re.sub(r"/\*.*?\*/", "", joined, flags=re.DOTALL)
    return joined


def interactive_requirements(commands, roots):
    """Which authored commands invoke a program that reads from the user.

    Returns a list of dicts {command, program, primitive}; empty when no
    authored command's resolved program source uses an input primitive.
    Resolution goes through resolve_source_file, i.e. Stata's own findfile
    inside Stata, so the program scanned is the one Stata would run.
    """
    found = []
    seen = set()
    authored_pause_on = False
    for command in commands:
        bare = strip_prefixes(command)
        token = command_token(bare)
        if token == "pause":
            # Stata's documented pause semantics, not a scan of pause.ado
            # (whose _request() IS the pause mechanism): `pause on|off` are
            # settings and never wait; any other pause waits only while pause
            # is on, i.e. after an authored `pause on` earlier in the example.
            if _PAUSE_ON_RE.search(bare):
                authored_pause_on = True
            elif re.match(r"^\s*pause\s+off\s*$", bare, re.IGNORECASE):
                authored_pause_on = False
            elif authored_pause_on:
                found.append({"command": command, "program": "(authored pause)",
                              "primitive": "pause"})
            continue
        if not token or token in seen or not IDENTIFIER_RE.match(token):
            continue
        seen.add(token)
        try:
            source = resolve_source_file(token + ".ado", roots)
        except Exception:
            source = None
        if source is None or not Path(source).is_file():
            continue
        try:
            text = _program_code_only(
                Path(source).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for label, pattern in _INTERACTIVE_INPUT_RES:
            if pattern.search(text):
                found.append({"command": command, "program": str(source),
                              "primitive": label})
                break
        else:
            # `pause` blocks only when pause is on: switched on by the program
            # itself or by the authored example
            if _PAUSE_CMD_RE.search(text) and (
                    _PAUSE_ON_RE.search(text) or authored_pause_on):
                found.append({"command": command, "program": str(source),
                              "primitive": "pause"})
    return found


# Trace lines, as Stata writes them with traceindent, tracenumber and
# tracesep off: a two-character prefix at column 1.
_TRACE_LINE_RE = re.compile(r"^[-=] ")
_TRACE_ON_ECHO_RE = re.compile(r"^\. set trace on\s*$")
_TRACE_OFF_ECHO_RE = re.compile(r"^\. set trace off\s*$")
_FENCE_ECHO_RE = re.compile(r"^\. \* (HELPRUN-(?:INTERNAL|AUTHORED)-(?:BEGIN|END))\s*$")
# A trace line that names an input primitive: the last thing Stata writes
# before a program blocks on the user.
_PAUSE_TRACE_RE = re.compile(
    r"^[-=] .*(?:\b_request\s*\(|\bpause\b|\bwindow\s+stopbox\b)",
    re.IGNORECASE)

TRACE_ON_COMMANDS = ("set trace on", "set tracedepth 32", "set traceexpand off",
                     "set traceindent off", "set tracenumber off",
                     "set tracesep off")
TRACE_OFF_COMMANDS = ("set trace off",)


def strip_trace_lines(text):
    """Drop the trace lines helprun's own bounded trace produced.

    Only the span between helprun's `set trace on` and `set trace off`, which
    are emitted inside INTERNAL fences, is affected: an authored program's own
    output is never touched outside that span, and inside it only lines with
    Stata's trace prefix are removed. This is a format rule scoped to
    instrumentation helprun itself injected, not a text rule on authored
    content. A traced source line of a branch that was NOT taken (for example
    a program's own "declined" message under a false `if`) is trace, not
    output, and is removed with the rest.
    """
    if not text:
        return text
    out = []
    internal = False
    tracing = False
    removed_previous = False
    for line in text.splitlines():
        m = _FENCE_ECHO_RE.match(line)
        if m:
            marker = m.group(1)
            if marker == INTERNAL_BEGIN:
                internal = True
            elif marker == INTERNAL_END:
                internal = False
            out.append(line)
            removed_previous = False
            continue
        if internal and _TRACE_ON_ECHO_RE.match(line):
            tracing = True
        elif internal and _TRACE_OFF_ECHO_RE.match(line):
            tracing = False
        elif tracing and (_TRACE_LINE_RE.match(line) or line.startswith("  ")):
            # with traceindent off, a traced NESTED line is written with a
            # two-space prefix and no dash; still trace, still removed
            removed_previous = True
            continue
        elif tracing and removed_previous and line.startswith("> "):
            # Stata wraps a long line onto "> " continuation lines; a wrap of
            # a removed trace line is trace too. A wrap of a KEPT line is kept.
            continue
        out.append(line)
        removed_previous = False
    return "\n".join(out)


def worker_window_title(topic, ordinal):
    """The visible worker's title: identifies it as helprun's, names the
    example, and says what to do. Fixed wording, no topic-specific logic."""
    return ("helprun worker -- %s, example %s -- when this window asks, answer "
            "in its Command box (press Enter to confirm)" % (topic, ordinal))


class InteractionCancelled(Exception):
    """The user cancelled while the example waited for an answer (Break in the
    parent, or a harness hook that declines). Nothing is answered on the
    user's behalf and the owner stops its worker."""


# Prompt classes (specification section 10). Decided from the prompt text the
# authored program displayed, to shape one line of guidance and the provenance
# record; the user's line is relayed verbatim whatever the class.
PROMPT_ENTER_ONLY = "ENTER_ONLY"
PROMPT_YES_NO = "YES_NO"
PROMPT_NUMBER = "NUMBER"
PROMPT_TEXT = "TEXT"
PROMPT_LINE = "LINE"
PROMPT_DIALOG = "DIALOG"   # a modal dialog: not a line, never relayed
_PROMPT_YES_NO_RE = re.compile(
    r"\[\s*y\s*/\s*n\s*\]|\(\s*y\s*/\s*n\s*\)|\byes\s*/\s*no\b|\by\s*/\s*n\b|\bconfirm\s*/\s*cancel\b",
    re.IGNORECASE)
_PROMPT_ENTER_RE = re.compile(
    r"\b(?:press|hit)\s+(?:enter|return)\b|\benter\s+to\s+(?:continue|apply|proceed|confirm)\b",
    re.IGNORECASE)
_PROMPT_NUMBER_RE = re.compile(r"\b(?:number|numeric|how\s+many|integer|count)\b", re.IGNORECASE)
_PROMPT_TEXT_RE = re.compile(
    r"\b(?:file\s*name|filename|name|path|directory|folder|label|text|string|word|title)\b",
    re.IGNORECASE)
_PROMPT_SECRET_RE = re.compile(r"\b(?:password|passphrase|secret|token|api\s*key|credential)\b",
                               re.IGNORECASE)


def classify_prompt(text):
    """One of the prompt classes for an authored prompt line.

    A prompt that asks for a VALUE is classified by the value it asks for, even
    when it also tells the user to press Enter afterwards. A real installed
    example asks "Type the number of the file to import, then press
    Enter." (anchor recorded in the ledger), and calling that
    ENTER-ONLY described the interaction wrongly wherever the class was shown
    (HPROD-58). The class only words the guidance and the internal record; the
    user's line is relayed verbatim whatever it says.
    """
    t = (text or "").strip()
    if not t:
        return PROMPT_LINE
    if _PROMPT_YES_NO_RE.search(t):
        return PROMPT_YES_NO
    if _PROMPT_NUMBER_RE.search(t):
        return PROMPT_NUMBER
    if _PROMPT_TEXT_RE.search(t):
        return PROMPT_TEXT
    if _PROMPT_ENTER_RE.search(t):
        return PROMPT_ENTER_ONLY
    return PROMPT_LINE


def prompt_text_from_log(log_text, since=0):
    """The authored prompt: the last non-empty displayed line since offset
    `since` that is neither a command echo nor a trace line."""
    for line in reversed([l for l in log_text[since:].splitlines() if l.strip()]):
        if not line.startswith(". ") and not _TRACE_LINE_RE.match(line):
            return line.strip()
    return ""


def _worker_top_windows(pid):
    if os.name != "nt" or not pid:
        return []
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    u32.EnumWindows.argtypes = [EnumProc, wt.LPARAM]
    u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    found = []

    def top(h, _l):
        d = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(d))
        if d.value == int(pid):
            found.append(int(h))
        return True

    u32.EnumWindows(EnumProc(top), 0)
    return found


def _worker_command_window(pid):
    """The Command window (a Scintilla control, R22-1) of the worker with THIS
    pid; 0 when not found. Only that process's windows are ever considered."""
    if os.name != "nt" or not pid:
        return 0
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    u32.EnumChildWindows.argtypes = [wt.HWND, EnumProc, wt.LPARAM]
    u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    found = []

    def child(h, _l):
        b = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(h, b, 64)
        if b.value == "Scintilla":
            found.append(int(h))
        return True

    for top in _worker_top_windows(pid):
        u32.EnumChildWindows(top, EnumProc(child), 0)
        if found:
            break
    return found[0] if found else 0


def relay_answer(pid, text):
    """Deliver the user's line to the worker with `pid` (R22-3, R22-4): one
    posted WM_CHAR per UTF-16 code unit into ITS Command window, then exactly
    one Enter (WM_KEYDOWN/WM_KEYUP VK_RETURN). No pointer crosses processes,
    nothing is sent anywhere but that control, and neither focus nor the
    foreground window changes. Returns {hwnd, delivered, why}."""
    hwnd = _worker_command_window(pid)
    if not hwnd:
        return {"hwnd": 0, "hwnd_pid": 0, "delivered": False,
                "why": "no Command window found for the worker"}
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    owner = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    if owner.value != int(pid):
        # never deliver anywhere but the waiting worker's own process
        return {"hwnd": hwnd, "hwnd_pid": int(owner.value), "delivered": False,
                "why": "the Command window found belongs to another process"}
    WM_KEYDOWN, WM_KEYUP, WM_CHAR, VK_RETURN = 0x0100, 0x0101, 0x0102, 0x0D
    raw = str(text).encode("utf-16-le")
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        u32.PostMessageW(hwnd, WM_CHAR, ctypes.c_void_p(unit), ctypes.c_void_p(1))
        time.sleep(0.01)
    time.sleep(0.2)
    u32.PostMessageW(hwnd, WM_KEYDOWN, ctypes.c_void_p(VK_RETURN), ctypes.c_void_p(0x001C0001))
    u32.PostMessageW(hwnd, WM_KEYUP, ctypes.c_void_p(VK_RETURN), ctypes.c_void_p(0xC01C0001))
    return {"hwnd": hwnd, "hwnd_pid": int(owner.value), "delivered": True, "why": ""}


def worker_window_visible(pid):
    """Is any top-level window of the worker visible?"""
    if os.name != "nt" or not pid:
        return False
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.IsWindowVisible.argtypes = [wt.HWND]
    u32.GetWindowTextLengthW.argtypes = [wt.HWND]
    return any(u32.IsWindowVisible(h) and u32.GetWindowTextLengthW(h) > 0
               for h in _worker_top_windows(pid))


def _foreground_pid():
    """The process that owns the foreground window (0 when unknown)."""
    if os.name != "nt":
        return 0
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetForegroundWindow.restype = wt.HWND
    u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    h = u32.GetForegroundWindow()
    if not h:
        return 0
    d = wt.DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(d))
    return int(d.value)


def worker_dialog_open(pid):
    """The handle of a visible modal dialog (window class #32770) owned by the
    worker with `pid`, else 0 (GATE 2 R25b). Stata's `window stopbox` shows one
    and disables the main window while it waits; its command echo stays in
    Stata's buffer, so the log alone cannot show this pause."""
    if os.name != "nt" or not pid:
        return 0
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u32.IsWindowVisible.argtypes = [wt.HWND]
    for h in _worker_top_windows(pid):
        b = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(h, b, 64)
        if b.value == "#32770" and u32.IsWindowVisible(h):
            return int(h)
    return 0


def reveal_worker(pid):
    """Show the hidden worker's main window: the minimum visible interaction,
    used only when an answer cannot be relayed (unsupported prompt class, no
    Command window, or a relay the worker did not act on)."""
    if os.name != "nt" or not pid:
        return False
    import ctypes
    import ctypes.wintypes as wt
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    u32.GetWindowTextLengthW.argtypes = [wt.HWND]
    shown = False
    for h in _worker_top_windows(pid):
        if u32.GetWindowTextLengthW(h) > 0:
            u32.ShowWindow(h, 1)   # SW_SHOWNORMAL, no activation request beyond Stata's own
            shown = True
    return shown


def strip_streamed_prefix(transcript, streamed_lines):
    """Drop from the front of the final transcript the lines the parent already
    showed while relaying prompts (compared non-blank line by non-blank line),
    so the Results bridge does not print them twice."""
    if not streamed_lines:
        return transcript
    wanted = [l.rstrip() for l in streamed_lines if l.strip()]
    if not wanted:
        return transcript
    lines = transcript.splitlines()
    i = j = 0
    while i < len(lines) and j < len(wanted):
        if not lines[i].strip():
            i += 1
            continue
        if lines[i].rstrip() != wanted[j]:
            break
        i += 1
        j += 1
    if j < len(wanted):
        return transcript   # not a clean prefix: show everything rather than lose a line
    return "\n".join(lines[i:]).lstrip("\n")


def last_echoed_command(log_text):
    """The most recent command Stata echoed into the log (`. command`)."""
    for line in reversed(log_text.splitlines()):
        if line.startswith(". ") and line.strip() != ".":
            return line[2:].strip()
    return ""


def _process_cpu_seconds(pid):
    """Kernel + user CPU time of a process, in seconds (0.0 when unknown).

    Used to tell a worker WAITING for the user (idle) from one computing:
    a paused interpreter consumes no CPU, a running one does.
    """
    if not pid or os.name != "nt":
        return 0.0
    try:
        import ctypes
        import ctypes.wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return 0.0
        try:
            c, e, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
            if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e),
                                       ctypes.byref(kt), ctypes.byref(ut)):
                return 0.0
            total = ((kt.dwHighDateTime << 32) + kt.dwLowDateTime
                     + (ut.dwHighDateTime << 32) + ut.dwLowDateTime)
            return total / 1e7
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return 0.0


def worker_pause_detected(log_text, quiet_seconds, input_commands=(), idle=True,
                          min_quiet=1.0):
    """Is the worker blocked on an input primitive right now?

    True when the log has not grown for `min_quiet` seconds and EITHER the
    last command Stata echoed is one of the authored commands whose program
    reads input (`input_commands`) while the process is idle (a waiting
    interpreter consumes no CPU; a computing one does), OR -- for a log that
    carries helprun's trace, as older plans did -- the last line is a trace
    line naming an input primitive. Nothing is ever sent to the worker on this
    basis; it only decides what the parent tells the user and whether the
    computation timeout is running.
    """
    lines = [l for l in log_text.splitlines() if l.strip()]

    # The settle applies to the traced branch too, and deliberately: a trace
    # line naming an input primitive says Stata ENTERED it, not that it is
    # blocked in it, and a primitive that returns at once -- `_request()` in
    # batch does -- would otherwise be read as a pause the instant it started.
    # INTER-09 fixes that in both directions. Lifting the settle here to make
    # detection load-independent was tried on 2026-09-06 and broke it, so the
    # load sensitivity recorded as HHARN-57 stays in the detector, and the
    # contract now reports BLOCKED when it cannot judge rather than passing or
    # failing on machine load.
    if not lines or quiet_seconds < min_quiet:
        return False
    # A do-file that has printed its epilogue has FINISHED. A worker that is
    # quiet and idle after that is winding down, not blocked on input.
    #
    # Without this the detector could fire a second time on an input command
    # that had already been answered: `paused` is cleared when the log grows,
    # but a program that finishes without printing anything more leaves the
    # same command as the last echoed one, so a quiet idle worker looked
    # exactly like a fresh pause. prompt_text_from_log then had only the
    # epilogue to offer, and the parent asked the user to answer
    # `end of do-file`. Intermittent by nature -- it needs the wind-down to
    # last past the settle -- which is why it survived as a rare INTER-20
    # failure rather than a reproducible one (HPROD-67).
    #
    # Only the unambiguous end-of-do-file marker is used. `r(N);`, the other
    # half of the batch epilogue, can appear mid-log after a captured failure,
    # and treating it as terminal could suppress a legitimate later pause.
    if lines[-1].strip().lower().startswith("end of do-file"):
        return False
    if _PAUSE_TRACE_RE.match(lines[-1]):
        return True
    if not idle:
        return False
    last = last_echoed_command(log_text)
    if not last:
        return False
    wanted = set()
    for command in input_commands or ():
        first = str(command).strip().splitlines()[0].strip() if str(command).strip() else ""
        if first:
            wanted.add(first)
    return last in wanted


# ============================================================
# Executor
# ============================================================

def _child_environment(sandbox):
    """Create a private temporary environment for one helprun execution."""
    temp_root = Path(sandbox) / "_tmp"
    temp_root.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    temp_text = str(temp_root)
    env["TEMP"] = temp_text
    env["TMP"] = temp_text
    env["TMPDIR"] = temp_text

    return env, temp_root


def _split_process_segments(commands):
    """
    Split one authored example into sequential hidden-Stata processes when
    a top-level exit command deliberately ends one process and commands
    follow it.  This supports documented recovery workflows without ever
    terminating the interactive parent Stata.

    exit inside program/Mata/Python/input definitions is not a process
    boundary.
    """
    segments = []
    current = []
    block_mode = None

    for command in commands:
        stripped = strip_prefixes(command).strip()
        low = stripped.lower()

        current.append(command)

        if block_mode is not None:
            if low == "end":
                block_mode = None
            continue

        if re.match(r"^mata\s*:$", low):
            block_mode = "mata"
            continue

        if re.match(r"^python\s*:$", low):
            block_mode = "python"
            continue

        if re.match(r"^program\s+(define|def)\b", low):
            block_mode = "program"
            continue

        if re.match(r"^input(?:\s|$)", low):
            block_mode = "input"
            continue

        if command_token(command) == "exit":
            segments.append(current)
            current = []

    if current:
        segments.append(current)

    return segments


def _run_segment_worker(exe, plan, sandbox, child_env, child_job,
                        timeout_seconds, worker):
    """Run one plan in an interactive Stata that helprun owns, HIDDEN by default.

    Returns (returncode, timed_out, completed). `completed` is whether the
    plan's authored region reached its end in the log; a worker the user
    closed early exits without it. The computation timeout runs only while the
    worker is working: while it is paused on an input primitive the clock is
    stopped. An ordinary prompt is presented in the parent through
    worker["ask"] and the user's line is relayed to THIS worker's Command
    window (HPROD-48); only a prompt that cannot be relayed reveals the
    worker, and the parent is then told, once, where to answer.
    """
    log_path = Path(sandbox) / str(worker.get("log", Path(plan).stem + ".log"))

    # `run`, not `do`: the wrapper's own lines are never echoed or displayed;
    # only the authored segment it runs noisily reaches the window (HPROD-47).
    # The worker starts HIDDEN (R24-1): ordinary prompts are answered in the
    # parent and relayed here; it is shown only when a prompt cannot be
    # relayed (HPROD-48).
    startup = None
    if os.name == "nt" and not worker.get("visible"):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0  # SW_HIDE
    p = subprocess.Popen(
        [str(exe), "run", str(plan)],
        cwd=str(sandbox),
        env=child_env,
        startupinfo=startup,
    )
    assign_process_to_job(child_job, p.pid)
    cpu_at_quiet = _process_cpu_seconds(p.pid)
    if worker.get("on_start"):
        try:
            worker["on_start"](p.pid)
        except Exception:
            pass

    active = 0.0
    waited = 0.0
    pauses = 0
    unanswered = False
    try:
        input_wait_limit = float(os.environ.get("HELPRUN_INPUT_WAIT_SECONDS", "0") or 0)
    except ValueError:
        input_wait_limit = 0.0
    last_tick = time.time()
    last_len = -1
    quiet_since = time.time()
    paused = False
    announced = False
    timed_out = False
    cancelled = False
    revealed = bool(worker.get("visible"))
    interactions = []
    streamed = []
    relay_offset = 0
    relayed_at = None
    ask = worker.get("ask")
    relayable = worker.get("relayable", True)

    while True:
        time.sleep(0.25)
        now = time.time()
        text = ""
        if log_path.is_file():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        if len(text) != last_len:
            last_len = len(text)
            quiet_since = now
            cpu_at_quiet = _process_cpu_seconds(p.pid)
            if paused and interactions and interactions[-1].get("resumed") is None:
                interactions[-1]["resumed"] = True
            paused = False
            relayed_at = None
        idle = (_process_cpu_seconds(p.pid) - cpu_at_quiet) < 0.05
        # A modal dialog is a pause the log cannot show (R25b): the worker owns
        # a visible #32770 window while its echo is still buffered.
        dialog = worker_dialog_open(p.pid) if (now - quiet_since) >= 1.0 else 0
        if not paused and dialog:
            paused = True
            pauses += 1
            interactions.append({
                "ordinal": pauses, "prompt": "modal dialog", "class": PROMPT_DIALOG,
                "answer": "(answered in the worker window)", "relayed_at": "-",
                "hwnd": dialog, "hwnd_pid": int(p.pid), "delivered": False,
                "resumed": None, "why": "a dialog cannot be relayed",
                # provenance for the focus hazard (0.6.2): did the worker's
                # dialog hold the foreground when it was noticed?
                "foreground": _foreground_pid() == int(p.pid)})
            if not revealed:
                revealed = reveal_worker(p.pid)
            if not announced and worker.get("on_pause"):
                announced = True
                try:
                    worker["on_pause"]()
                except Exception:
                    pass
        elif not paused and worker_pause_detected(text, now - quiet_since,
                                                  worker.get("input_commands", ()), idle):
            paused = True
            pauses += 1
            prompt = prompt_text_from_log(text, relay_offset)
            kind = classify_prompt(prompt)
            delta = text[relay_offset:]
            if ask is not None and relayable:
                # PARENT-MEDIATED (HPROD-48): show what the example displayed
                # since the last answer, ask in the parent, relay the line to
                # THIS worker only. `ask` returns the user's line, or None when
                # nobody is there to answer (unattended: the bounded wait
                # below applies), or raises InteractionCancelled.
                info = {"ordinal": pauses, "prompt": prompt, "class": kind,
                        "transcript": delta, "pid": p.pid, "title": worker.get("title", "")}
                try:
                    answer = ask(info)
                except InteractionCancelled:
                    cancelled = True
                    terminate_job(child_job)
                    try:
                        p.kill()
                    except OSError:
                        pass
                    p.wait()
                    break
                streamed.extend(delta.splitlines())
                relay_offset = len(text)
                if answer is not None:
                    res = relay_answer(p.pid, answer)
                    secret = bool(_PROMPT_SECRET_RE.search(prompt or ""))
                    interactions.append({
                        "ordinal": pauses, "prompt": prompt, "class": kind,
                        "answer": ("(withheld)" if secret else str(answer)),
                        "relayed_at": time.strftime("%H:%M:%S"), "hwnd": res.get("hwnd", 0),
                        "hwnd_pid": res.get("hwnd_pid", 0),
                        "delivered": bool(res.get("delivered")), "resumed": None,
                        "why": res.get("why", "")})
                    relayed_at = now
                    if not res.get("delivered") and not revealed:
                        revealed = reveal_worker(p.pid)
                        if worker.get("on_pause"):
                            try:
                                worker["on_pause"]()
                            except Exception:
                                pass
            elif not revealed:
                # not relayable (a modal dialog, or no way to ask): the minimum
                # visible interaction -- show the worker and say where to answer
                revealed = reveal_worker(p.pid)
                if not announced and worker.get("on_pause"):
                    announced = True
                    try:
                        worker["on_pause"]()
                    except Exception:
                        pass
        if paused and relayed_at is not None and now - relayed_at > 15 and not revealed:
            # the worker did not act on a delivered answer within 15 s: reveal it
            interactions[-1]["resumed"] = False
            revealed = reveal_worker(p.pid)
            if worker.get("on_pause"):
                try:
                    worker["on_pause"]()
                except Exception:
                    pass
        if not paused:
            active += now - last_tick
        else:
            waited += now - last_tick
        last_tick = now
        if p.poll() is not None:
            if interactions and interactions[-1].get("resumed") is None:
                interactions[-1]["resumed"] = True
            break
        if paused and input_wait_limit > 0 and waited > input_wait_limit:
            # Nobody answered within the configured bound. The owner stops its
            # own worker; nothing is answered on the user's behalf.
            unanswered = True
            terminate_job(child_job)
            try:
                p.kill()
            except OSError:
                pass
            p.wait()
            break
        if active > timeout_seconds:
            timed_out = True
            terminate_job(child_job)
            try:
                p.kill()
            except OSError:
                pass
            p.wait()
            break

    text = ""
    if log_path.is_file():
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    # The wrapper writes the marker only after the authored region ran to its
    # end; an authored error or a window closed by the user leaves none.
    completed = (Path(sandbox) / WORKER_DONE_MARKER).is_file()
    worker["pauses"] = pauses
    worker["waited_seconds"] = round(waited, 1)
    worker["pid"] = p.pid
    worker["unanswered"] = unanswered
    worker["cancelled"] = cancelled
    worker["revealed"] = revealed
    worker["interactions"] = interactions
    worker["streamed_lines"] = streamed
    return p.returncode, timed_out, completed


def execute_units(
    exe,
    selected_units,
    label,
    source_dir=None,
    roots=None,
    timeout_seconds=90,
    capture=None,
    staged_inputs=None,
    worker=None,
    trace_commands=()
):
    commands = []

    for unit in selected_units:
        commands.extend(unit["code"])

    for command in commands:
        if interactive_command(command):
            return {
                "status": STATUS_REFUSED,
                "reason": "USER_INTERACTION_REQUIRED",
                "child": False,
                "pass": False,
                "child_pid": None,
                "sandbox": None,
                "r_codes": [],
                "error": (
                    "helprun: this example needs a command that requires the "
                    "Stata interface: " + command
                ),
            }

    segments = _split_process_segments(commands)

    if not segments:
        return {
            "status": "REFUSE_EMPTY",
            "child": False,
            "pass": False,
            "child_pid": None,
            "sandbox": None,
            "r_codes": [],
            "error": "helprun: no executable commands remained after planning",
        }

    if len(segments) > 4:
        return {
            "status": "REFUSE_PROCESS_BOUNDARY",
            "child": False,
            "pass": False,
            "child_pid": None,
            "sandbox": None,
            "r_codes": [],
            "error": "helprun: example requires more than four child Stata processes",
        }

    sandbox = Path(
        tempfile.mkdtemp(
            prefix="helprun_" + label + "_"
        )
    )

    # Authored data inputs the caller already resolved. Resolution alone is not
    # enough: the child runs in the sandbox, so a file the PARENT could see at
    # a relative path is invisible to it, and a legitimate authored input then
    # reports DATA_FILE_MISSING with r(601) purely because execution moved. The
    # resolved file is copied in under the name the example uses.
    #
    # An input is never overwritten if the sandbox already holds that name: a
    # package dependency staged under the same name wins, because it is the one
    # the example's own package shipped.
    for _name, _source_path in (staged_inputs or []):
        try:
            _target = sandbox / Path(_name).name
            if not _target.exists():
                shutil.copyfile(_source_path, _target)
        except OSError:
            # A file that cannot be copied is left unresolved rather than
            # silently substituted; the child then fails on the authored
            # command, which is the honest outcome.
            pass

    try:
        _stage_package_dependencies(
            commands,
            source_dir if source_dir is not None else Path.cwd(),
            roots if roots is not None else [],
            sandbox
        )
    except Exception as exc:
        return {
            "status": STATUS_REFUSED,
            "reason": "PACKAGE_FILE_MISSING",
            "child": False,
            "pass": False,
            "child_pid": None,
            "sandbox": str(sandbox),
            "r_codes": [],
            "error": str(exc),
        }

    child_env, child_temp = _child_environment(sandbox)

    # Files present before execution, so only what the example itself created
    # can ever be treated as an authored artifact.
    pre_existing = {
        str(p.relative_to(sandbox)).lower()
        for p in sandbox.rglob("*")
        if p.is_file()
    }

    capture_dir = None
    if capture:
        capture_dir = sandbox / "_hr_out"
        capture_dir.mkdir(parents=True, exist_ok=True)

    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE

    creation_flags = getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0
    )

    child_pids = []
    all_r_codes = []
    log_parts = []

    # One job per clicked run.  Every child Stata of this run joins it, so a
    # timeout can terminate the whole process tree rather than orphaning
    # grandchildren (GATE 2 R19).
    child_job = create_child_job()

    # The graph snapshot helper is an ado-file in the sandbox, reached through
    # the adopath the preamble appends. It must exist before any plan runs, and
    # it must be a FILE: an authored `clear all` drops the loaded program, and
    # Stata reloads it from here on the next call (HPROD-66).
    if capture_dir is not None:
        try:
            (sandbox / "_hr_gsnap.ado").write_text(
                "\n".join(graph_capture_helper_ado()) + "\n", encoding="utf-8")
        except OSError:
            pass

    for index, segment in enumerate(segments, start=1):
        if len(segments) == 1:
            plan = sandbox / "plan.do"
        else:
            plan = sandbox / ("plan_" + str(index) + ".do")

        if capture_dir is not None:
            segment_capture = {
                "out_dir": capture_dir,
                "basename": (
                    capture["basename"]
                    if len(segments) == 1
                    else capture["basename"] + "-part" + str(index)
                ),
            }
        else:
            segment_capture = None

        trace_indices = [i for i, c in enumerate(segment)
                         if c in set(trace_commands)]
        worker_spec = None
        if worker is not None:
            worker_spec = dict(worker)
            worker_spec["log"] = plan.stem + ".log"
            worker_spec["input_commands"] = tuple(trace_commands)

        plan.write_text(
            "\n".join(build_child_plan(segment, segment_capture,
                                       trace_indices)) + "\n",
            encoding="utf-8",
        )

        timeout = False
        worker_completed = True

        if worker is not None:
            # An example whose program reads input runs in a VISIBLE Stata the
            # user can answer in (HPROD-42). Same fenced plan, same sandbox,
            # same log file name; the plan is run through a fenced wrapper
            # do-file so the worker logs, survives an authored error, echoes
            # Stata's return code, and exits by itself.
            # The authored segment alone, as one nested do-file the silent
            # wrapper runs noisily: the worker's window and log show exactly
            # the authored commands and their output (HPROD-47).
            authored = sandbox / (plan.stem + "_authored.do")
            authored.write_text(chr(10).join(segment) + chr(10), encoding="utf-8")
            wrapper = sandbox / (plan.stem + "_worker.do")
            wrapper.write_text(
                chr(10).join(build_worker_wrapper(worker_spec, authored.name,
                                                  segment_capture)) + chr(10),
                encoding="utf-8",
            )

            class _Worker(object):
                pass
            p = _Worker()
            p.pid = None
            p.returncode, timeout, worker_completed = _run_segment_worker(
                exe, wrapper, sandbox, child_env, child_job, timeout_seconds,
                worker_spec)
        else:
            p = subprocess.Popen(
                [
                    str(exe),
                    "/e",
                    "/q",
                    "/i",
                    "do",
                    str(plan),
                ],
                cwd=str(sandbox),
                env=child_env,
                startupinfo=startup,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            child_pids.append(p.pid)
            assign_process_to_job(child_job, p.pid)

            try:
                p.wait(timeout=timeout_seconds)

            except subprocess.TimeoutExpired:
                timeout = True

                # Terminate the whole job first: killing only the direct child
                # would leave any grandchild it spawned running (GATE 2 R19).
                terminate_job(child_job)

                try:
                    p.kill()
                except OSError:
                    pass

                p.wait()

        segment_log_file = sandbox / (plan.stem + ".log")

        if segment_log_file.exists():
            segment_log = segment_log_file.read_text(
                encoding="utf-8",
                errors="replace"
            )
        else:
            segment_log = ""

        if worker is not None and segment_log:
            # The worker's log is the authored region alone; give it the
            # fences every downstream filter selects by, and keep the file
            # consistent with what the filters see.
            segment_log = fence_worker_log(segment_log, worker_completed)
            try:
                segment_log_file.write_text(segment_log, encoding="utf-8")
            except OSError:
                pass

        segment_r_codes = re.findall(
            r"(?m)^r\(([0-9]+)\);",
            segment_log
        )

        all_r_codes.extend(segment_r_codes)

        if len(segments) > 1:
            log_parts.append(
                "================ helprun child process "
                + str(index)
                + " / "
                + str(len(segments))
                + " ================\n"
                + segment_log
            )

        segment_pass = (
            not timeout
            and (p.returncode == 0 or worker is not None)
            and segment_log_file.exists()
            and not segment_r_codes
            and worker_completed
        )

        if (worker is not None and not timeout and not worker_completed
                and not segment_r_codes):
            # The worker exited before the authored region ended and without
            # an error of its own: either nobody answered within the
            # configured bound (HELPRUN_INPUT_WAIT_SECONDS) and the owner
            # stopped its worker, or the user closed the window. Nothing was
            # confirmed on the user's behalf and this is not reported as
            # SUCCESS. (An authored error is classified from the log like any
            # other run.)
            close_job(child_job)
            if worker_spec.get("cancelled"):
                message = (
                    "helprun: the example was waiting for your answer and the run "
                    "was cancelled; the worker was closed and nothing was confirmed "
                    "on your behalf"
                )
            elif worker_spec.get("unanswered"):
                message = (
                    "helprun: the example asked for your answer and none was given "
                    "within %s seconds; the worker was closed and nothing was "
                    "confirmed on your behalf"
                    % os.environ.get("HELPRUN_INPUT_WAIT_SECONDS", "")
                )
            else:
                message = (
                    "helprun: the example was waiting for your answer in its "
                    "worker window, and that window closed before the example "
                    "completed; nothing was confirmed on your behalf"
                )
            return {
                "status": STATUS_FAILED,
                "reason": "INTERACTIVE_INPUT_REQUIRED",
                "child": True,
                "pass": False,
                "child_pid": None,
                "child_pids": child_pids,
                "sandbox": str(sandbox),
                "temp_root": str(child_temp),
                "logfile": str(segment_log_file) if segment_log_file.exists() else "",
                "r_codes": all_r_codes,
                "pre_existing": pre_existing,
                # the pause the user saw is evidence on EVERY path (HHARN-43a)
                "interactive": _worker_evidence(worker, worker_spec),
                "error": message,
            }

        if not segment_pass:
            if len(segments) > 1:
                (sandbox / "plan.log").write_text(
                    "\n\n".join(log_parts) + "\n",
                    encoding="utf-8",
                )

            if timeout:
                error = (
                    "helprun: child Stata process "
                    + str(index)
                    + " timed out"
                )
            elif segment_r_codes:
                error = (
                    "helprun: child Stata process "
                    + str(index)
                    + " failed with r("
                    + segment_r_codes[-1]
                    + ")"
                )
            elif not segment_log_file.exists():
                error = (
                    "helprun: child Stata process "
                    + str(index)
                    + " produced no execution log"
                )
            else:
                error = (
                    "helprun: child Stata process "
                    + str(index)
                    + " exited with code "
                    + str(p.returncode)
                )

            combined_log = sandbox / "plan.log"

            # For a single segment the plan is already named plan.log, so the
            # combined log IS the segment log and no copy is needed.
            if (
                len(segments) == 1
                and segment_log_file.exists()
                and segment_log_file.resolve() != combined_log.resolve()
            ):
                shutil.copyfile(segment_log_file, combined_log)

            close_job(child_job)

            return {
                "status": "TIMEOUT" if timeout else "EXECUTE",
                # Only the timeout is classifiable from here. An ordinary child
                # failure is classified by the caller from the log evidence:
                # labelling every failure HELP_CODE_ERROR would assert the
                # author's code was at fault without evidence, and would hide
                # network, version and data provenance.
                "reason": "EXECUTION_TIMEOUT" if timeout else "",
                "child": True,
                "pass": False,
                # a worker run records no batch child pid (HPROD-42)
                "child_pid": child_pids[-1] if child_pids else None,
                "child_pids": child_pids,
                "sandbox": str(sandbox),
                "temp_root": str(child_temp),
                "logfile": str(combined_log) if combined_log.exists() else "",
                "r_codes": all_r_codes,
                "pre_existing": pre_existing,
                # the pause the user saw is evidence on EVERY path (HHARN-43a)
                "interactive": _worker_evidence(worker, worker_spec),
                "error": error,
            }

        # A top-level exit boundary is intentionally complete only after
        # this child has fully terminated.  The next segment, if any,
        # therefore starts in a genuinely fresh Stata process.

    if len(segments) > 1:
        (sandbox / "plan.log").write_text(
            "\n\n".join(log_parts) + "\n",
            encoding="utf-8",
        )

    close_job(child_job)

    return {
        "status": "EXECUTE",
        "reason": "",
        "child": True,
        "pass": True,
        "child_pid": child_pids[-1] if child_pids else None,
        "child_pids": child_pids,
        "sandbox": str(sandbox),
        "temp_root": str(child_temp),
        "logfile": str(sandbox / "plan.log"),
        "r_codes": all_r_codes,
        "pre_existing": pre_existing,
        "child_temp": str(child_temp),
        "segments": len(segments),
        "interactive": _worker_evidence(worker, worker_spec),
        "error": "",
    }


def _worker_evidence(worker, worker_spec):
    """What the persistent log records about a run's interactive worker.

    Returned on EVERY path out of execute_units -- success, authored failure,
    unanswered, cancelled or closed worker -- so a run that paused for the
    user and then ended FAILED still carries its INTERACTIVE and PROMPT header
    lines (HHARN-43a, HPROD-48). None for a hidden batch child.
    """
    if worker is None or worker_spec is None:
        return None
    return {"title": worker_spec.get("title", ""),
            "pauses": worker_spec.get("pauses", 0),
            "waited_seconds": worker_spec.get("waited_seconds", 0),
            "pid": worker_spec.get("pid"),
            "hidden": not worker_spec.get("visible", False),
            "revealed": bool(worker_spec.get("revealed")),
            "cancelled": bool(worker_spec.get("cancelled")),
            "interactions": list(worker_spec.get("interactions") or []),
            "streamed_lines": list(worker_spec.get("streamed_lines") or [])}


# ============================================================
# Public-surface provenance
#
# Every line or event that can reach Results, a persistent log, a diagnostic or
# an artifact manifest carries a class, and the public surfaces admit only the
# permitted classes -- by class, never by matching particular strings.
#
# WHY THIS EXISTS
#
# Hygiene used to be a growing list of forbidden tokens: `_hr_gsnap`, the region
# fences, the click echo, the worker surface, and most recently the relay
# orchestration. A token list can only refuse what someone has already seen, so
# each new internal line was public until a person noticed it -- the INTERACTIVE
# and PROMPT header lines were invented by this project and were therefore never
# candidates for refusal, and they reached the user's persistent log (HPROD-57,
# HPROD-58). Classifying at the point of production inverts that default:
# something new is internal until its provenance says otherwise.
#
# The token list survives as a REGRESSION ANCHOR in tests/public_surface.py; it
# is no longer the rule.
# ============================================================

PROVENANCE_CLASSES = (
    # public
    "AUTHORED_COMMAND",       # a command the help author wrote
    "AUTHORED_OUTPUT",        # output of an authored Stata command
    "RUNTIME_OUTPUT",         # output of a runtime the authored example invoked
    "USER_PROMPT",            # a genuine prompt the user must answer
    "USER_RESPONSE_SUMMARY",  # concise confirmation that an interaction occurred
    "HELPRUN_NOTICE",         # helprun's own concise user-facing statement
    # internal
    "HELPRUN_INTERNAL",       # orchestration, helpers, bookkeeping, instrumentation
    "HARNESS_INTERNAL",       # validation driver, automation, mutation, debug
)

PUBLIC_PROVENANCE = (
    "AUTHORED_COMMAND", "AUTHORED_OUTPUT", "RUNTIME_OUTPUT",
    "USER_PROMPT", "USER_RESPONSE_SUMMARY", "HELPRUN_NOTICE",
)

# Producers, as the code that emits a line names itself. A writer that is not
# listed yields no class at all, which the public surfaces refuse: uncertain
# provenance is never silently relabelled as authored output.
_WRITER_CLASS = {
    "authored": "AUTHORED_OUTPUT",
    "runtime": "RUNTIME_OUTPUT",
    "prompt": "USER_PROMPT",
    "interaction_summary": "USER_RESPONSE_SUMMARY",
    "helprun_user_facing": "USER_RESPONSE_SUMMARY",
    "helprun_notice": "HELPRUN_NOTICE",
    "helprun_internal": "HELPRUN_INTERNAL",
    "harness": "HARNESS_INTERNAL",
}


def public_surface_admits(provenance):
    """May a line of this provenance appear in Results or the persistent log?"""
    return provenance in PUBLIC_PROVENANCE


def provenance_of(line, context=None):
    """The provenance class of one line, from WHO PRODUCED IT.

    `context["writer"]` is the producing path's own name. The text is consulted
    only to separate an authored command echo from authored output, which is a
    property of the transcript Stata itself wrote (`. command`), never a guess
    about what a line means. An unknown writer returns "UNKNOWN", which
    public_surface_admits refuses.
    """
    ctx = context or {}
    writer = str(ctx.get("writer", "")).strip().lower()
    cls = _WRITER_CLASS.get(writer)

    if cls is None:
        return "UNKNOWN"

    if cls == "AUTHORED_OUTPUT":
        if str(line).startswith(". "):
            return "AUTHORED_COMMAND"

        # An authored line that asks the reader for an answer is a prompt, and
        # naming it one is what lets a surface treat prompts as prompts. Both
        # classes are public, so this refines the label and can never admit
        # something a stricter reading would have refused.
        #
        # Only the unmistakable shapes count here. classify_prompt's word tests
        # are deliberately not used: they exist to word the guidance for a line
        # ALREADY known to be a prompt because the worker stopped for it, and
        # they would read "number of observations" in ordinary output as a
        # request for input.
        if _PROMPT_YES_NO_RE.search(str(line)) or _PROMPT_ENTER_RE.search(str(line)):
            return "USER_PROMPT"

    return cls


def interactive_header_lines(interactive):
    """The user-facing record that the example asked the user something.

    Provenance USER_RESPONSE_SUMMARY: it confirms that an interaction occurred
    and that nothing was answered on the user's behalf. The orchestration behind
    it -- the worker's identity and visibility, the internal prompt class, the
    relayed text, relay timestamps, pause counts, wait durations and resumption
    state -- is HELPRUN_INTERNAL and stays on the outcome for validation, off
    the user's surfaces (HPROD-57/58). The authored prompt itself is already in
    the run's own output, where the author put it.
    """
    if not interactive or not interactive.get("pauses"):
        return []

    if interactive.get("cancelled"):
        return ["INTERACTION : this example asked you a question and the run was cancelled; "
                "nothing was answered on your behalf."]

    delivered = [i for i in (interactive.get("interactions") or []) if i.get("delivered")]

    if delivered:
        return ["INTERACTION : this example asked you a question here and your answer was applied."]

    return ["INTERACTION : this example asked you a question; nothing was answered on your behalf."]


def interactive_internal_record(interactive):
    """The full orchestration record, provenance HELPRUN_INTERNAL.

    Returned on the outcome for validation and reproducibility. It is never
    written to a public surface; a clean public log is achieved by classifying
    this material, not by deleting the evidence the harness needs.
    """
    if not interactive:
        return []
    record = [
        "worker=%s visible=%s pauses=%s waited_seconds=%s cancelled=%s"
        % (interactive.get("title", ""),
           bool(interactive.get("revealed")) or not interactive.get("hidden"),
           interactive.get("pauses", 0), interactive.get("waited_seconds", 0),
           bool(interactive.get("cancelled")))]
    for it in interactive.get("interactions") or []:
        resumed = it.get("resumed")
        record.append(
            "prompt %d: class=%s text=%r answer=%r relayed_at=%s delivered=%s %s"
            % (it.get("ordinal", 0), it.get("class", ""),
               redact_secrets(str(it.get("prompt", "")))[:160],
               redact_secrets(str(it.get("answer", ""))), it.get("relayed_at", ""),
               bool(it.get("delivered")),
               "resumed" if resumed else ("not resumed" if resumed is False else "pending")))
    return record


# ============================================================
# Dependency evidence and prerequisite planning
#
# The planner selects prerequisites from static evidence about what each unit
# creates and uses, plus the actual variable list of the dataset a unit loads.
# It never runs an example speculatively to find out whether it works:
# specification section 6 requires only the necessary prerequisites, and
# section 13 requires capture to be behaviour-preserving. Repeated trial
# executions violate both, and would also execute code the user never clicked.
# ============================================================

DATA_LOAD_RE = re.compile(
    r"^(sysuse|webuse|use|import\s+\S+|infile|insheet|odbc\s+load)\b",
    flags=re.IGNORECASE,
)

VARLIST_COMMANDS = {
    "summarize", "summ", "su", "sum",
    "regress", "reg",
    "list", "describe", "desc",
    "tabulate", "tab",
    "correlate", "corr",
    "mean", "total",
    "histogram", "scatter",
    "sort", "gsort", "keep",
}

STATA_NON_VARIABLE_WORDS = {
    "if", "in", "using", "by", "bysort", "with", "and", "or", "not",
    "clear", "replace", "all", "_all", "_n", "_pi",
}

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def unit_loads_data(unit):
    for command in unit["code"]:
        s = command.strip()
        if DATA_LOAD_RE.match(s):
            return True
        if re.match(r"^input\b", s, flags=re.IGNORECASE):
            return True
    return False


DATA_GENERATING_RE = re.compile(
    r"^(set\s+obs|input\b|drawnorm|generate|gen|egen|matrix\s+\w+\s*=)\b",
    flags=re.IGNORECASE,
)

USER_DATA_INSTRUCTION_RE = re.compile(
    r"\b(your\s+(own\s+)?data|use\s+your|substitute\s+your|"
    r"replace\s+.*with\s+your)\b",
    flags=re.IGNORECASE,
)


def example_provides_data_setup(unit, doc_lines=()):
    """Does this structural Example supply anything to run against?

    Section 9's EXAMPLE_DATA_SETUP_MISSING requires source-side evidence that
    the Example provides no dataset, no data-generating setup, no package/cwd/
    URL data source and no legitimate user-data instruction. Without that
    evidence an arbitrary r(111) must NOT be remapped to this reason -- that is
    the negative control the frozen mutant M39 exists to enforce.

    This is a structural audit of the authored source. No help topic, package
    or command name takes part in the decision.
    """
    for command in unit.get("code", []):
        s = command.strip()

        if DATA_LOAD_RE.match(s):
            return True
        if DATA_GENERATING_RE.match(s):
            return True
        # A download that stages a dataset is a data source.
        if re.match(r"^copy\s+\S+", s, flags=re.IGNORECASE) and re.search(
            r"\.(dta|csv|xlsx?|txt)\b", s, flags=re.IGNORECASE
        ):
            return True
        # An explicit frame/preserve of existing data still needs a source, so
        # those alone do not count.

    # A help page that tells the reader to bring their own data is not a
    # missing-setup defect; it is USER_DATA_REQUIRED and is handled elsewhere.
    for line in doc_lines or ():
        if USER_DATA_INSTRUCTION_RE.search(str(line)):
            return True

    return False


def continues_earlier_example(target, units):
    """Is this unit, structurally, a continuation of an earlier example?

    Every example runs in its own Stata session, so nothing an earlier example
    left behind -- the dataset in memory, e()/r() results, frames, a command's
    own undo buffer, Mata or Python objects -- is present when a later example
    runs on its own. A unit that establishes no session of its own, in a
    document where an earlier unit does, is a continuation by construction.

    This is a structural reading of the authored document, and it is reported
    ALONGSIDE the real Stata error, never instead of it: it says what helprun
    knows about the document, and makes no causal claim about the failure. No
    help topic, package or command name takes part in the decision.
    """
    if not target or not units:
        return False

    try:
        ordinal = int(target.get("ordinal") or 0)
    except (TypeError, ValueError):
        return False

    if ordinal <= 1:
        return False

    # The document-level user-data instruction is deliberately not consulted
    # here: it is a property of the whole page and would answer the same for
    # every unit, which would tell these two halves apart from nothing.
    if example_provides_data_setup(target, ()):
        return False

    for unit in units:
        try:
            other = int(unit.get("ordinal") or 0)
        except (TypeError, ValueError):
            continue

        if other < ordinal and example_provides_data_setup(unit, ()):
            return True

    return False


NO_DATA_PHRASES = (
    "no variables defined",
    "no observations",
    "data in memory would be lost",
)


def unit_data_source(unit):
    """The dataset a unit loads, as (kind, name), or (None, None)."""
    for command in unit["code"]:
        s = command.strip()

        m = re.match(r"^(sysuse|webuse)\s+([^\s,]+)", s, flags=re.IGNORECASE)
        if m:
            return m.group(1).lower(), _unquote_path(m.group(2))

        m = re.match(r'^use\s+("[^"]+"|[^\s,]+)', s, flags=re.IGNORECASE)
        if m:
            return "use", _unquote_path(m.group(1))

    return None, None


def unit_creates(unit):
    """Names a unit brings into existence: variables, macros, stored results."""
    created = set()

    for command in unit["code"]:
        s = command.strip()

        m = re.match(
            r"^(?:quietly\s+|qui\s+|noisily\s+|noi\s+)*"
            r"(?:gen|generate|egen)\b[^=]*?([A-Za-z_][A-Za-z0-9_]*)\s*=",
            s,
            flags=re.IGNORECASE,
        )
        if m:
            created.add(m.group(1))
            continue

        m = re.match(
            r"^rename\s+\S+\s+([A-Za-z_][A-Za-z0-9_]*)",
            s,
            flags=re.IGNORECASE,
        )
        if m:
            created.add(m.group(1))
            continue

        m = re.match(
            r"^(?:local|global|scalar|matrix|tempvar|tempname|tempfile)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            s,
            flags=re.IGNORECASE,
        )
        if m:
            created.add(m.group(1))
            continue

        m = re.match(
            r"^(?:est|estimates)\s+store\s+([A-Za-z_][A-Za-z0-9_]*)",
            s,
            flags=re.IGNORECASE,
        )
        if m:
            created.add(m.group(1))
            continue

        m = re.search(
            r"\bgen\(([A-Za-z_][A-Za-z0-9_]*)\)", s, flags=re.IGNORECASE
        )
        if m:
            created.add(m.group(1))
            continue

        m = re.match(r"^input\s+(.+)$", s, flags=re.IGNORECASE)
        if m:
            for token in re.split(r"\s+", m.group(1).strip()):
                if IDENTIFIER_RE.match(token):
                    created.add(token)
            continue

        m = re.match(
            r"^frame\s+create\s+([A-Za-z_][A-Za-z0-9_]*)",
            s,
            flags=re.IGNORECASE,
        )
        if m:
            created.add(m.group(1))

    return created


def unit_referenced_names(unit):
    """Plain identifiers a unit mentions, as cross-unit dependency candidates."""
    names = set()

    for command in unit["code"]:
        s = command.strip()

        if s.startswith("*") or s.startswith("//"):
            continue

        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", s):
            names.add(token)

    return names


# Commands after which the variable set is whatever Stata makes it.
#
# WHY A BOUNDARY AND NOT A LONGER LIST OF PRODUCERS
#
# The planner used to require every identifier a unit mentions in a varlist
# position, minus a list of names it could see being created -- gen, egen,
# rename, input and a few others. That list can never be complete. collapse
# invents _freq and the statistic variables; contract invents _freq; reshape
# invents the wide or long forms; merge brings in the whole using dataset;
# a later use, webuse or import replaces the variable set outright. Every
# omission becomes a false refusal of a perfectly good example, and the real
# a real installed help Example was refused for exactly that reason -- for
# annual_range, cities, demand_type, mean_degree_days, median_degree_days,
# percentage and total_degree_days, all of which the example itself produces.
#
# Extending the producer list is the wrong shape of fix: it would have to
# anticipate every command that can add a variable, which is a Stata
# interpreter, and the specification forbids building one.
#
# So the rule is inverted and bounded. A variable is a genuine INITIAL
# prerequisite only while the planner can still know what the dataset holds --
# that is, until the first command that can change it. From that point the
# authored sequence is authoritative and the planner stops requiring anything,
# because Stata is the authority on evolving dataset state. This names no
# command that produces any particular variable; it names the point after
# which the planner has no standing to judge.
# A command that LOADS a dataset establishes a state the planner can check
# against: it knows which file was loaded and can ask Stata for its varlist.
DATASET_LOADERS = frozenset({
    "use", "u", "sysuse", "webuse", "import", "infile", "insheet", "odbc",
})

# A command after which the variable set is whatever Stata makes it. These do
# not establish a state the planner can inspect -- they transform one.
DATASET_MUTATORS = frozenset({
    "collapse", "contract", "reshape", "merge", "append", "joinby", "cross",
    "expand", "fillin", "stack", "xpose", "separate", "split",
    "generate", "gen", "egen", "rename", "ren", "drop", "keep",
    "encode", "decode", "destring", "tostring", "recode", "input",
    "restore", "nestrestore", "clear", "frame", "frames",
})

_OPTION_CREATES_RE = re.compile(
    r"(?:gen|generate)\s*\(", flags=re.IGNORECASE)


def _leading_command(line):
    s = line.strip()
    if not s or s.startswith("*") or s.startswith("//"):
        return ""
    s = re.sub(r"^(?:quietly|qui|noisily|noi|capture|cap)\s+", "", s,
               flags=re.IGNORECASE)

    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", s)
    first = m.group(1).lower() if m else ""

    # Resolve the leading word BEFORE any colon handling. `merge 1:1 make
    # using other` contains a colon that belongs to the match specification,
    # not to a prefix command; treating it as one discarded the merge entirely
    # and let a merged-in variable be demanded up front. A command that already
    # names itself needs no prefix interpretation.
    if first in DATASET_LOADERS or first in DATASET_MUTATORS:
        return first

    # Otherwise a prefix command may carry its own colon; judge what follows.
    if ":" in s:
        head, _, rest = s.partition(":")
        # A prefix head carries a varlist, options and parentheses --
        # `bysort id (t):`, `svy, subpop(male):`. Restricting it to bare words
        # meant those fell through to the prefix word itself, so an egen or
        # collapse behind a by-prefix was never seen as a state change.
        if re.match(r"^[A-Za-z_][A-Za-z0-9_ ,()*?~.=<>!&|/+-]*$",
                    head.strip()):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", rest.strip())
            return m.group(1).lower() if m else ""

    return first


def first_state_change(unit):
    """Index of the first command after which the variable set is unknown.

    WHY A BOUNDARY AND NOT A LONGER LIST OF PRODUCERS

    The planner used to require every identifier a unit mentions in a varlist
    position, minus a list of names it could see being created -- gen, egen,
    rename, input and a few others. That list can never be complete. collapse
    invents _freq and the statistic variables; contract invents _freq; reshape
    invents the wide or long forms; merge brings in the whole using dataset.
    Every omission becomes a false refusal of a good example, and the real
    a real installed help Example was refused for exactly that reason -- for
    annual_range, cities, demand_type, mean_degree_days, median_degree_days,
    percentage and total_degree_days, every one of which it produces itself.

    Extending the producer list is the wrong shape of fix: it would have to
    anticipate every command that can add a variable, which is a Stata
    interpreter, and the specification forbids building one.

    So the rule is inverted and bounded. The FIRST dataset load establishes a
    state the planner can inspect, and it keeps its standing to judge until the
    authored sequence transforms that state -- a mutator, or a second load of a
    different dataset. After that point Stata is authoritative, exactly as the
    frozen rule says, and the planner requires nothing further. This names no
    command that produces any particular variable; it names the point after
    which the planner has no standing.
    """
    seen_loader = False

    for i, command in enumerate(unit["code"]):
        word = _leading_command(command)
        if not word:
            continue

        if word in DATASET_LOADERS:
            if seen_loader:
                # a second, different dataset: the earlier varlist no longer
                # describes what is in memory
                return i
            seen_loader = True
            continue

        if word in DATASET_MUTATORS:
            return i

        if _OPTION_CREATES_RE.search(command):
            return i

    return len(unit["code"])


def judgeable_prefix(unit):
    """The unit, truncated at the first command that can change the data.

    Only this prefix may be used to decide what the example needs BEFORE it
    runs. Everything after it is the authored sequence doing its work, and
    Stata decides whether that succeeds.
    """
    cut = first_state_change(unit)
    return {"code": list(unit["code"])[:cut]}


def unit_varlist_candidates(unit):
    """Identifiers used in a varlist position of a recognised data command.

    Deliberately conservative: only plain identifiers, only for recognised
    commands, stopping at the first comma or if/in/using qualifier.
    Factor-variable, wildcard and range forms are skipped rather than guessed
    at, so the planner never refuses an example merely because it could not
    parse an expression.
    """
    candidates = set()

    for command in unit["code"]:
        s = command.strip()

        s = re.sub(
            r"^(?:quietly|qui|noisily|noi)\s+",
            "",
            s,
            flags=re.IGNORECASE,
        )

        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(.*)$", s)
        if not m:
            continue

        if m.group(1).lower() not in VARLIST_COMMANDS:
            continue

        rest = re.split(
            r",|\bif\b|\bin\b|\busing\b", m.group(2), maxsplit=1
        )[0]

        for token in re.split(r"\s+", rest.strip()):
            if not token or not IDENTIFIER_RE.match(token):
                continue
            if token.lower() in STATA_NON_VARIABLE_WORDS:
                continue
            candidates.add(token)

    return candidates


def dataset_variables(source_kind, source_name, roots):
    """Variable names of a dataset, obtained from Stata itself.

    Returns None when the dataset cannot be resolved with confidence, in which
    case the planner draws no conclusion from it.
    """
    if not source_name or not stata_available():
        return None

    name = source_name
    if not re.search(r"\.dta$", name, flags=re.IGNORECASE):
        name = name + ".dta"

    path = resolve_source_file(name, roots)

    if path is None or not Path(path).exists():
        return None

    try:
        from sfi import SFIToolkit, Macro

        SFIToolkit.stata(
            'quietly capture describe using "' + str(path) + '", varlist'
        )
        varlist = Macro.getGlobal("r(varlist)")
    except Exception:
        return None

    if not varlist:
        return None

    return {v for v in re.split(r"\s+", varlist.strip()) if v}


def plan_prerequisites(units, target, roots):
    """Select only the units the target requires, in authored order.

    Returns (plan, problem). When a genuine dependency cannot be resolved the
    plan is empty and nothing is executed; brute-force execution of every
    preceding example is exactly what specification section 6 forbids.
    """
    by_ordinal = {u["ordinal"]: u for u in units}
    creates = {u["ordinal"]: unit_creates(u) for u in units}
    loads = {u["ordinal"]: unit_loads_data(u) for u in units}

    creator_of = {}
    for unit in units:
        for name in creates[unit["ordinal"]]:
            creator_of.setdefault(name, unit["ordinal"])

    selected = set()

    def require(ordinal, depth=0):
        if ordinal in selected or depth > 32:
            return
        selected.add(ordinal)

        for name in unit_referenced_names(by_ordinal[ordinal]):
            origin = creator_of.get(name)
            if origin is not None and origin < ordinal:
                require(origin, depth + 1)

    require(target["ordinal"])

    needs_data = bool(unit_varlist_candidates(judgeable_prefix(target))) or any(
        re.match(
            r"^(gen|generate|egen|replace)\b", c.strip(), flags=re.IGNORECASE
        )
        for c in target["code"]
    )

    if needs_data and not any(loads[o] for o in selected):
        loaders = [
            u["ordinal"]
            for u in units
            if u["ordinal"] < target["ordinal"] and loads[u["ordinal"]]
        ]
        if loaders:
            require(max(loaders))

    plan = [by_ordinal[o] for o in sorted(selected)]

    # Evidence-based unmet-dependency check. It fires only when the dataset's
    # variable list is genuinely known, so an unresolvable dataset can never
    # cause a false refusal.
    # A unit may load SEVERAL datasets in turn -- base/r/regress.sthlp's second
    # example loads auto, then `webuse regsmpl`, and its later variables come
    # from regsmpl. Checking only the first dataset refused that example
    # outright. Every dataset the plan loads therefore contributes, and if any
    # of them cannot be resolved no conclusion is drawn at all.
    loader = next((u for u in plan if loads[u["ordinal"]]), None)
    sources = []

    for unit in plan:
        if not loads[unit["ordinal"]]:
            continue
        for command in unit["code"]:
            probe = dict(code=[command])
            kind, name = unit_data_source(probe)
            if kind is not None:
                sources.append((kind, name))

    known_vars = None

    if sources:
        union = set()
        for kind, name in sources:
            got = dataset_variables(kind, name, roots)
            if got is None:
                union = None
                break
            union |= got
        known_vars = union

    if known_vars is not None:
        available = set(known_vars)
        for unit in plan:
            available |= creates[unit["ordinal"]]

        unmet = sorted(
            token
            for token in unit_varlist_candidates(judgeable_prefix(target))
            if token not in available and token not in creator_of
        )

        if unmet:
            # Which explanation the evidence supports depends on who named the
            # dataset. When the example itself loads a specific file and that
            # file lacks the variables the example documents, the mismatch is
            # between the help and that data. When the example relies on an
            # earlier example instead, the prerequisite is what is unresolved.
            if loader is not None and loader["ordinal"] == target["ordinal"]:
                _kind, named = unit_data_source(loader)
                return [], HelprunError(
                    "HELP_DATA_MISMATCH",
                    "helprun: "
                    + str(named)
                    + " does not contain "
                    + ", ".join(unmet)
                    + ", which this example requires",
                    detail="unmet=" + ",".join(unmet),
                )

            return [], HelprunError(
                "UNRESOLVED_PREREQUISITE",
                "helprun: this example needs "
                + ", ".join(unmet)
                + ", which no earlier example creates and the loaded dataset "
                "does not contain",
                detail="unmet=" + ",".join(unmet),
            )

    return plan, None


# ============================================================
# Guard: role and provenance, never an extension blacklist
#
# Specification section 7. The decision dimensions are the dependency role,
# its provenance, the source, the target path, runtime availability and
# whether the action modifies the system persistently. A file extension is
# never by itself a reason to refuse: an installed module legitimately ships
# .py, .jar, .dll, .exe and .do components (section 7.1).
# ============================================================

GUARD_ALLOW = "ALLOW"
GUARD_CONFIRM = "CONFIRM"
GUARD_REFUSE = "REFUSE"

DEP_STRUCTURAL = "STRUCTURAL_PREDECESSOR"
DEP_DATA_SETUP = "DATA_SETUP"
DEP_FILE_PACKAGE = "FILE_PACKAGE_DEPENDENCY"
DEP_EXPLICIT_RUNTIME = "EXPLICIT_RUNTIME_DEPENDENCY"

DEPENDENCY_CLASSES = frozenset(
    {DEP_STRUCTURAL, DEP_DATA_SETUP, DEP_FILE_PACKAGE, DEP_EXPLICIT_RUNTIME}
)

PERSISTENT_INSTALL_RE = re.compile(
    r"^\s*(?:ssc\s+install|ssc\s+hot|net\s+install|net\s+get|adoupdate|"
    r"update\s+all|python\s+.*\bpip\s+install|shell\s+.*\bpip\s+install)\b",
    flags=re.IGNORECASE,
)

EXTERNAL_LAUNCH_RE = re.compile(
    r"^\s*(?:shell|winexec|!|javacall|plugin\s+call|python\s+script)\b",
    flags=re.IGNORECASE,
)

NETWORK_COPY_RE = re.compile(
    r"^\s*copy\s+(?P<src>\S+)\s+(?P<dst>\"[^\"]+\"|\S+)", flags=re.IGNORECASE
)


def _is_within(path, parents):
    try:
        resolved = Path(os.path.normpath(str(path))).resolve()
    except OSError:
        return False

    for parent in parents:
        if not parent:
            continue
        try:
            resolved.relative_to(Path(os.path.normpath(str(parent))).resolve())
            return True
        except (ValueError, OSError):
            continue

    return False


def _authorised_write_roots(ctx):
    roots = [ctx.get("sandbox"), ctx.get("out_dir")]
    return [r for r in roots if r]


def _package_provenance(token, ctx):
    """Is this referenced component an installed package file?

    Provenance is evidence about where a component came from, not a claim that
    it is safe (specification section 7.5).
    """
    raw = _unquote_path(token)

    if not raw:
        return None

    source_dir = ctx.get("source_dir")
    roots = ctx.get("roots") or []

    if source_dir:
        candidate = Path(source_dir) / raw
        if candidate.exists():
            return str(candidate)

    resolved = resolve_source_file(raw, roots)
    if resolved is not None and Path(resolved).exists():
        return str(resolved)

    for root in roots:
        candidate = Path(root) / raw
        if candidate.exists():
            return str(candidate)

    return None


# The package a persistent-install command would install. General across the
# install forms Stata actually offers; it names no package.
_INSTALL_TARGET_RE = re.compile(
    r"^\s*(?:ssc\s+(?:install|hot)|net\s+(?:install|get))\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    flags=re.IGNORECASE,
)


def dependency_already_usable(command, ctx):
    """Is the dependency this install command provides already available?

    Specification U-R13: an already-usable dependency must not be reinstalled,
    and no confirmation may be requested merely because an install command
    appears in the Example. R20E separates already-usable from declined,
    approved-but-unverified and approved-verified; only the last resumes.

    Usability is decided by the same authoritative resolution helprun uses
    everywhere else, which inside Stata is Stata's own `findfile`. That keeps
    the answer identical to the one the child process would get, and keeps this
    rule free of any package name.
    """
    match = _INSTALL_TARGET_RE.match(command.strip())

    if not match:
        return None

    name = match.group(1)
    roots = ctx.get("roots") or []

    for ext in (".ado", ".sthlp"):
        found = resolve_source_file(name + ext, roots)
        if found is not None and Path(found).exists():
            return name

    return None


# ============================================================
# PERSISTENT DEPENDENCY WORKFLOW  (specification section 7.3, GATE 2 R20E)
# ============================================================
#
# R20E established by controlled observation that the four states are
# distinguishable without guessing:
#
#   already usable            findfile/which succeed BEFORE any install
#   missing                   findfile fails; an install would be required
#   approved but unverified   the install returns, yet the dependency is still
#                             unresolvable, so nothing became usable
#   approved and verified     the dependency resolves after the install
#
# Only the last may resume the clicked Example. The decision is a pure function
# so it can be exercised without installing anything: the caller injects how a
# dependency is looked up and how an install is performed. Production passes the
# real Stata-backed probes; tests pass controlled ones and never touch the
# user's installation.

DEP_ALREADY_USABLE = "ALREADY_USABLE"
DEP_MISSING = "MISSING"
DEP_DECLINED = "DECLINED"
DEP_APPROVED_UNVERIFIED = "APPROVED_UNVERIFIED"
DEP_APPROVED_VERIFIED = "APPROVED_VERIFIED"

DEP_ACTION_SKIP = "SKIP"
DEP_ACTION_CONFIRM = "CONFIRM"
DEP_ACTION_STOP = "STOP"
DEP_ACTION_FAIL = "FAIL"
DEP_ACTION_RESUME = "RESUME"


def dependency_workflow(command, ctx, approved=None,
                        is_usable=None, do_install=None):
    """One persistent-install command, resolved to a state and an action.

    `approved` is None until the user has been asked: the workflow then returns
    CONFIRM and stops, which is what makes the confirmation a real boundary
    rather than something inferred. True or False carry the user's actual answer.

    `is_usable(name)` and `do_install(command)` are injected so the state
    machine can be tested exhaustively without performing a persistent change.
    Production supplies the authoritative Stata-backed versions.
    """
    if is_usable is None:
        def is_usable(name):
            return dependency_already_usable("ssc install " + name, ctx) is not None

    match = _INSTALL_TARGET_RE.match(command.strip())
    name = match.group(1) if match else ""

    if not name:
        return {"state": "", "action": "", "package": "", "resume": False}

    def result(state, action, resume=False, evidence=""):
        return {
            "state": state,
            "action": action,
            "package": name,
            "resume": resume,
            "command": command.strip(),
            "evidence": evidence,
        }

    # 1. Already usable: nothing to install and nothing to ask.
    if is_usable(name):
        return result(DEP_ALREADY_USABLE, DEP_ACTION_SKIP,
                      evidence=name + " is already available")

    # 2. Missing and not yet answered: the user must be asked, and the exact
    #    bounded change is what they are asked about.
    if approved is None:
        return result(DEP_MISSING, DEP_ACTION_CONFIRM,
                      evidence="would install " + name)

    # 3. Declined: stop cleanly, without executing the Example.
    if not approved:
        return result(DEP_DECLINED, DEP_ACTION_STOP,
                      evidence="user declined the persistent change")

    # 4. Approved: perform only the approved operation, then VERIFY. R20E state
    #    3 showed an install can return and still leave the dependency
    #    unusable, so a returned install is never taken as success.
    if do_install is not None:
        do_install(command)

    if not is_usable(name):
        return result(DEP_APPROVED_UNVERIFIED, DEP_ACTION_FAIL,
                      evidence=name + " is still unusable after the install")

    return result(DEP_APPROVED_VERIFIED, DEP_ACTION_RESUME, resume=True,
                  evidence=name + " verified usable")


def resume_plan(commands, completed):
    """The commands still to run after an approved, verified install.

    Section 7.3 forbids duplicating already completed authored commands. The
    confirmation happens before any authored command executes, so `completed`
    is normally empty and the whole plan resumes; the parameter exists so a
    partial run can never be replayed from the top.
    """
    done = list(completed or [])
    remaining = list(commands)

    for finished in done:
        if remaining and remaining[0] == finished:
            remaining.pop(0)

    return remaining


def guard_decision(command, ctx):
    """Classify one reconstructed command by role, provenance and target."""
    s = command.strip()

    decision = {
        "command": s,
        "decision": GUARD_ALLOW,
        "reason": "",
        "dependency_class": DEP_STRUCTURAL,
        "role": "command",
        "required": True,
        "fallback_available": False,
        "provenance": "",
        "evidence": "",
        # A command the plan must not execute, though nothing is wrong with it.
        "skip": False,
    }

    if not s or s.startswith("*") or s.startswith("//"):
        return decision

    if DATA_LOAD_RE.match(s):
        decision["dependency_class"] = DEP_DATA_SETUP

    # An install whose dependency is already usable is neither a persistent
    # change nor a question for the user: there is nothing to install. Running
    # it anyway would reinstall over a working copy, which is exactly the
    # persistent modification the confirmation exists to prevent, so the
    # command is skipped rather than allowed through.
    already = dependency_already_usable(s, ctx) if PERSISTENT_INSTALL_RE.match(s) else None

    if already:
        decision.update(
            decision=GUARD_ALLOW,
            dependency_class=DEP_EXPLICIT_RUNTIME,
            role="dependency_already_usable",
            skip=True,
            provenance="resolved",
            evidence="%s is already available; nothing is installed" % already,
        )
        return decision

    # Persistent installation or configuration always needs confirmation.
    if PERSISTENT_INSTALL_RE.match(s):
        decision.update(
            decision=GUARD_CONFIRM,
            reason="USER_CONFIRMATION_REQUIRED",
            dependency_class=DEP_EXPLICIT_RUNTIME,
            role="persistent_install",
            evidence="command would persistently modify the installation",
        )
        return decision

    # Network copy is judged by scheme, role and target, never blanket-refused.
    m = NETWORK_COPY_RE.match(s)
    if m:
        src = _unquote_path(m.group("src"))
        dst = _unquote_path(m.group("dst"))
        decision["dependency_class"] = DEP_DATA_SETUP
        decision["role"] = "network_copy"
        decision["provenance"] = src

        if _unsafe_external_path(dst) and not _is_within(
            dst, _authorised_write_roots(ctx)
        ):
            decision.update(
                decision=GUARD_REFUSE,
                reason="UNSAFE_OPERATION_REFUSED",
                evidence="download target is outside the authorised output "
                "and sandbox boundary: " + dst,
            )
            return decision

        if src.lower().startswith("http://"):
            decision.update(
                decision=GUARD_CONFIRM,
                reason="USER_CONFIRMATION_REQUIRED",
                evidence="source is plain HTTP rather than HTTPS",
            )
            return decision

        return decision

    # Launching an external component.
    if EXTERNAL_LAUNCH_RE.match(s):
        decision["dependency_class"] = DEP_EXPLICIT_RUNTIME
        decision["role"] = "external_component"

        # Skip the launching command's own subcommand words so the component
        # itself is what gets provenance-checked: `python script helper.py`,
        # `plugin call helper.dll`, `javacall helper.jar main`.
        launcher_subwords = {"script", "call", "using", "query", "set"}

        tokens = re.findall(r'"[^"]+"|\S+', s)
        target_token = ""

        for token in tokens[1:]:
            candidate = _unquote_path(token)
            if candidate.startswith("-"):
                continue
            if candidate.lower() in launcher_subwords:
                continue
            target_token = candidate
            break

        decision["evidence"] = "component=" + target_token

        provenance = _package_provenance(target_token, ctx)

        if provenance:
            # An installed package component. Its extension is irrelevant.
            decision["provenance"] = "installed_package:" + provenance
            decision["dependency_class"] = DEP_FILE_PACKAGE
            return decision

        # Distinguish "installed but without package provenance", which is a
        # trust question for the user, from "not present anywhere", which is
        # simply a missing runtime and needs no confirmation prompt.
        absolute = _unsafe_external_path(target_token)
        present = False

        if target_token:
            try:
                present = bool(shutil.which(target_token)) or Path(
                    target_token
                ).exists()
            except OSError:
                present = False

        if not present:
            decision.update(
                decision=GUARD_REFUSE,
                reason="RUNTIME_MISSING",
                provenance="unresolved",
                evidence="required external component is not installed "
                "anywhere helprun can see: " + target_token,
            )
            return decision

        if absolute:
            decision.update(
                decision=GUARD_CONFIRM,
                reason="USER_CONFIRMATION_REQUIRED",
                provenance="unverified_absolute_path",
                evidence="binary at an absolute path with no installed-package "
                "provenance: " + target_token,
            )
            return decision

        decision.update(
            decision=GUARD_CONFIRM,
            reason="USER_CONFIRMATION_REQUIRED",
            provenance="unresolved",
            evidence="external component could not be resolved to an "
            "installed package file: " + target_token,
        )
        return decision

    # Writes outside the authorised boundary.
    for token in _quoted_paths(s):
        raw = _unquote_path(token)
        if _unsafe_external_path(raw) and not _is_within(
            raw, _authorised_write_roots(ctx)
        ):
            if re.match(
                r"^\s*(save|export|outfile|outsheet|graph\s+export|"
                r"putdocx\s+save|putexcel\s+set|file\s+open|erase|rm|"
                r"mkdir|rmdir|copy)\b",
                s,
                flags=re.IGNORECASE,
            ):
                decision.update(
                    decision=GUARD_REFUSE,
                    reason="UNSAFE_OPERATION_REFUSED",
                    evidence="write target is outside the authorised output "
                    "and sandbox boundary: " + raw,
                )
                return decision

    return decision


def guard_plan(commands, ctx):
    """Guard every reconstructed command; return (decisions, blocking)."""
    decisions = [guard_decision(command, ctx) for command in commands]

    blocking = [
        d for d in decisions if d["decision"] in (GUARD_REFUSE, GUARD_CONFIRM)
    ]

    return decisions, blocking


def detect_optional_accelerator(lines):
    """Recognise an optional accelerator declared in the help prose.

    An accelerator is an attribute of an explicit runtime dependency, not a new
    dependency class (specification section 6), and not a safety failure
    (section 7.2).
    """
    text = " ".join(str(line) for line in lines)
    low = text.lower()

    if "accelerat" not in low and "much faster" not in low:
        return None

    fallback = any(
        phrase in low
        for phrase in (
            "falls back",
            "fall back",
            "without",
            "if it is absent",
            "if absent",
            "slower",
        )
    )

    if not fallback:
        return None

    m = re.search(
        r"\{cmd:([^}]+?\.exe)\}|\b([A-Za-z0-9_.-]+\.exe)\b", text
    )
    name = ""
    if m:
        name = (m.group(1) or m.group(2) or "").strip()

    return {
        "dependency_class": DEP_EXPLICIT_RUNTIME,
        "role": "accelerator",
        "required": False,
        "fallback_available": True,
        "component": name,
    }


# ============================================================
# Run state: concurrency and bounded temporary-view cleanup
# ============================================================

RUN_LOCK_MAX_AGE_SECONDS = 3600
VIEW_DIR_PREFIX = "helprun_view_"
VIEW_MAX_AGE_SECONDS = 24 * 3600
VIEW_MAX_KEEP = 20


def helprun_state_dir():
    directory = Path(tempfile.gettempdir()) / "helprun_state"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _run_lock_path():
    return helprun_state_dir() / ("run_" + str(os.getpid()) + ".lock")


def acquire_run_lock():
    """One parent Stata process runs at most one helprun execution.

    A second click while one is active is refused with HELPRUN_BUSY and is not
    queued (specification section 14).
    """
    lock = _run_lock_path()

    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0.0

        if age < RUN_LOCK_MAX_AGE_SECONDS:
            raise HelprunError(
                "HELPRUN_BUSY",
                "helprun: another example is still running in this Stata "
                "session; wait for it to finish and click again",
            )

    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def release_run_lock():
    try:
        _run_lock_path().unlink()
    except OSError:
        pass


def cleanup_stale_views(now=None):
    """Bounded cleanup of transformed-Viewer temporary directories.

    They must not accumulate without bound (specification section 4). The
    newest are kept so a Viewer the user still has open keeps working.
    """
    base = Path(tempfile.gettempdir())
    now = time.time() if now is None else now

    try:
        candidates = [
            d for d in base.glob(VIEW_DIR_PREFIX + "*") if d.is_dir()
        ]
    except OSError:
        return 0

    def mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    candidates.sort(key=mtime, reverse=True)

    removed = 0

    for index, directory in enumerate(candidates):
        too_old = (now - mtime(directory)) > VIEW_MAX_AGE_SECONDS
        too_many = index >= VIEW_MAX_KEEP

        if too_old or too_many:
            try:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
            except OSError:
                pass

    return removed


# ============================================================
# Secret handling
#
# helprun may report that a secret is required or was provided, but must never
# echo or persist the value itself in Results, the persistent log, validation
# evidence, or release artifacts (specification section 9). This is not a
# credential manager and it does not try to guess which arbitrary help text is
# a secret; it redacts the values it itself carries.
# ============================================================

SECRET_OPTION_RE = re.compile(
    r"\b(password|passwd|pwd|token|apikey|api_key|secret|credential)"
    r"\s*\(\s*([^)]*)\)",
    flags=re.IGNORECASE,
)

SECRET_PLACEHOLDER = "<redacted by helprun>"


def redact_secrets(text, extra_values=()):
    """Mask secret values in anything helprun is about to show or persist."""
    if not text:
        return text

    redacted = SECRET_OPTION_RE.sub(
        lambda m: m.group(1) + "(" + SECRET_PLACEHOLDER + ")", str(text)
    )

    for value in extra_values:
        if value:
            redacted = redacted.replace(str(value), SECRET_PLACEHOLDER)

    return redacted


def command_requires_credential(command):
    return bool(SECRET_OPTION_RE.search(command or ""))


# ============================================================
# Preflight: requirements knowable before execution
#
# Specification section 17: diagnose what can be known reliably before
# running, rather than letting the example fail obscurely in a hidden child.
# ============================================================

LICENSE_PHRASES = (
    "licence",
    "license",
)

LICENSE_QUALIFIERS = (
    "require",
    "valid",
    "key",
    "activat",
    "entitle",
)


def help_declares_license(lines):
    """A licence requirement declared in the help prose, or None.

    Requires both a licence word and a qualifier such as "requires" or "valid",
    so ordinary prose mentioning a licence in passing does not trigger it.
    """
    text = " ".join(str(line) for line in lines)
    low = text.lower()

    if not any(word in low for word in LICENSE_PHRASES):
        return None

    if not any(word in low for word in LICENSE_QUALIFIERS):
        return None

    m = re.search(r"\{cmd:([^}]+)\}", text)
    component = m.group(1).strip() if m else ""

    return {"component": component}


def stata_version_number():
    """The running Stata's version as a float, or None outside Stata."""
    if not stata_available():
        return None

    try:
        from sfi import Macro

        return float(Macro.getGlobal("c(stata_version)"))
    except Exception:
        return None


VERSION_REQUIREMENT_RE = re.compile(
    r"^\s*version\s+([0-9]+(?:\.[0-9]+)?)\s*(?::|$)", flags=re.IGNORECASE
)


def required_stata_version(commands):
    """The highest Stata version the example's own code demands, or None."""
    highest = None

    for command in commands:
        m = VERSION_REQUIREMENT_RE.match(command.strip())
        if not m:
            continue
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        if highest is None or value > highest:
            highest = value

    return highest


# ============================================================
# Child failure classification
#
# Specification section 9: report the strongest failure explanation the
# evidence supports, and do not guess when provenance is ambiguous. An
# authored Stata error is never reported as a helprun internal error.
# ============================================================

FILE_NOT_FOUND_CODES = {"601", "603", "693"}

# GATE 4 probe (validation/g4_probe_net.log): a copy from an unresolvable host
# returns r(631) with "host not found"; a version requirement newer than the
# installed Stata returns r(9).
NETWORK_FAILURE_CODES = {"631", "672", "677", "679"}

# What Stata says when the requirement really is the version. Anything else
# returning r(9) -- `assert` above all -- is not a version problem (HPROD-63).
_VERSION_FAILURE_RE = re.compile(
    r"requires? version|this is version .* of stata|"
    r"version \d+(?:\.\d+)? is required|not supported by this version",
    flags=re.IGNORECASE,
)

NETWORK_FAILURE_PHRASES = (
    "host not found",
    "could not connect",
    "unable to connect",
    "connection timed out",
    "no such host",
    "server refused",
    "web resource not found",
)


def classify_child_failure(log_text, r_codes, known_missing_vars=None,
                           no_data_setup=False):
    """Map a child failure to (reason, evidence line) from real evidence.

    `no_data_setup` is the SOURCE-side finding from example_provides_data_setup().
    It is required, not optional: an arbitrary r(111) must never be rewritten as
    EXAMPLE_DATA_SETUP_MISSING without it.
    """
    text = log_text or ""
    low = text.lower()

    error_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith(". ")
    ]

    evidence = ""
    for line in reversed(error_lines):
        # Stata's own do-file framing is the last thing in every failed child
        # log, so taking the last line verbatim reported "end of do-file" as
        # though it were the error and buried the real message one line above.
        if line.lower().startswith("end of do-file") or line.lower().startswith(
            "end of file"
        ):
            continue

        if line and not line.startswith("r("):
            evidence = line
            break

    code = r_codes[-1] if r_codes else ""

    # A network failure is specific evidence and outranks the generic
    # file-not-found reading, because `copy` from a URL reports both.
    if code in NETWORK_FAILURE_CODES or any(
        phrase in low for phrase in NETWORK_FAILURE_PHRASES
    ):
        return "NETWORK_RESOURCE_UNAVAILABLE", evidence

    if code == "9":
        # r(9) is shared. GATE 4's probe saw it for a version requirement newer
        # than the installation, which is true; `assert` returns it too, and
        # `assert` is ordinary authored code. Reading the code alone told a
        # user whose example made a false assertion to go and check their Stata
        # version (HPROD-63) -- a confident wrong answer, which section 9
        # forbids more strongly than it forbids saying "I cannot tell".
        #
        # A version requirement is also caught BEFORE the run, by the preflight
        # in click_run, so an r(9) that reaches here is unlikely to be one; the
        # transcript has to say so.
        if _VERSION_FAILURE_RE.search(low):
            return "STATA_VERSION_INCOMPATIBLE", evidence

        if "assertion is false" in low:
            return "HELP_CODE_ERROR", evidence

    if code in FILE_NOT_FOUND_CODES or "file not found" in low:
        return "DATA_FILE_MISSING", evidence

    if code == "198":
        # Invalid syntax or option in the authored example text.
        return "HELP_CODE_ERROR", evidence

    if code == "199" or "unrecognized command" in text.lower():
        # Could be a missing package, a help typo, or an uninstalled
        # dependency. The evidence does not distinguish them.
        return "AMBIGUOUS_FAILURE_PROVENANCE", evidence

    if code == "111":
        if known_missing_vars:
            return "HELP_DATA_MISMATCH", evidence

        # Section 9: a data-dependent Example that supplies no dataset, no
        # data-generating setup, no package/cwd/URL source and no user-data
        # instruction, whose child really did hit the no-data condition, is
        # EXAMPLE_DATA_SETUP_MISSING. Both halves are required -- the runtime
        # no-data result AND the source-side absence of setup -- so an
        # unrelated r(111) is never remapped.
        if no_data_setup and any(p in low for p in NO_DATA_PHRASES):
            return "EXAMPLE_DATA_SETUP_MISSING", evidence

        return "AMBIGUOUS_FAILURE_PROVENANCE", evidence

    return "AMBIGUOUS_FAILURE_PROVENANCE", evidence


# ============================================================
# Cross-process in-memory dependencies
# ============================================================

def cross_process_macro_dependency(segments):
    """Detect in-memory state an authored process boundary cannot carry.

    Locals, r()/e() results, frames, Mata and Python objects do not survive a
    new process (specification section 10). Where the example genuinely needs
    them across the boundary, say so rather than faking continuity.
    """
    if len(segments) < 2:
        return None

    defined_before = set()

    for index, segment in enumerate(segments):
        used = set()
        for command in segment:
            for name in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)'", command):
                used.add(name)

        if index > 0:
            defined_here = set()
            for command in segment:
                m = re.match(
                    r"^\s*(?:local|tempvar|tempname|tempfile)\s+"
                    r"([A-Za-z_][A-Za-z0-9_]*)",
                    command,
                    flags=re.IGNORECASE,
                )
                if m:
                    defined_here.add(m.group(1))

            unmet = sorted(
                name
                for name in used
                if name in defined_before and name not in defined_here
            )

            if unmet:
                return unmet

        for command in segment:
            m = re.match(
                r"^\s*(?:local|tempvar|tempname|tempfile)\s+"
                r"([A-Za-z_][A-Za-z0-9_]*)",
                command,
                flags=re.IGNORECASE,
            )
            if m:
                defined_before.add(m.group(1))

    return None


# ============================================================
# Data dependency resolution
#
# Specification section 8 distinguishes three materially different situations:
# resolvable, the help expects the user's own data, and the help references a
# dataset no source provides. A fuzzily similar filename is never substituted.
# ============================================================

USER_DATA_PHRASES = (
    "your own data",
    "your own dataset",
    "substitute your own",
    "supply your own",
    "use your data",
)

DATA_REF_RE = re.compile(
    r'^\s*use\s+("[^"]+"|[^\s,]+)', flags=re.IGNORECASE
)


def help_expects_user_data(lines):
    text = " ".join(str(line) for line in lines).lower()
    return any(phrase in text for phrase in USER_DATA_PHRASES)


# A copy destination that names a DIRECTORY rather than a file. Measured in
# Stata 19.5 (validation/experiments/copy_destination.log):
#
#   copy "../src/d.dta" .        -> ./d.dta          destination is a directory
#   copy "src/d.dta"    "dst3/"  -> dst3/d.dta       trailing separator likewise
#   copy "src/d.dta"    "dst2"   -> a FILE named dst2, created even though no
#                                   such file existed beforehand
#
# so only the unambiguous forms are treated as directories. A bare name is the
# file name, which is what Stata does and what the previous reading assumed for
# every destination.
_DIRECTORY_DESTINATION_RE = re.compile(r"^(?:\.|\.\.)$|[\\/]\s*$")


def copy_destination_names(source, destination):
    """The name(s) `copy source destination` creates, lowercased.

    Reading the destination literally reported `copy <url> ., replace` as
    creating a file called `.`, so an example that downloads its own dataset
    and then reads it looked like an example reading a file nothing provides,
    and was refused before it ran (HPROD-65). The real anchor example named in
    that ledger row does exactly this.
    """
    destination = str(destination or "").strip()
    source = str(source or "").strip()

    if not destination:
        return set()

    if not _DIRECTORY_DESTINATION_RE.search(destination):
        return {destination.lower()}

    basename = re.split(r"[\\/]", source.rstrip("/\\"))[-1]

    if not basename:
        return set()

    names = {basename.lower()}

    if destination not in (".", ""):
        names.add((destination.rstrip("/\\") + "/" + basename).lower())

    return names


def example_created_files(commands):
    """Files the example itself writes earlier in the same run.

    A dataset the example saves and then re-reads -- the ordinary pattern for
    an authored process boundary -- is not a missing dependency.
    """
    created = set()

    for command in commands:
        s = command.strip()

        m = re.match(r'^save\s+("[^"]+"|[^\s,]+)', s, flags=re.IGNORECASE)
        if m:
            created.add(_unquote_path(m.group(1)).lower())
            continue

        m = re.search(
            r'\busing\s+("[^"]+"|[^\s,]+)', s, flags=re.IGNORECASE
        )
        if m and re.match(
            r"^(export|outfile|outsheet|save)", s, flags=re.IGNORECASE
        ):
            created.add(_unquote_path(m.group(1)).lower())
            continue

        # `copy <source> <destination>` produces the destination, so a later
        # `use` of it is reading what this example just downloaded or copied,
        # not a missing package file.
        m = re.match(
            r'^copy\s+("[^"]+"|\S+)\s+("[^"]+"|[^\s,]+)',
            s,
            flags=re.IGNORECASE,
        )
        if m:
            created.update(
                copy_destination_names(
                    _unquote_path(m.group(1)), _unquote_path(m.group(2))
                )
            )

    normalised = set()
    for name in created:
        normalised.add(name)
        if not re.search(r"\.\w+$", name):
            normalised.add(name + ".dta")

    return normalised


def resolve_data_references(commands, ctx, unit_lines):
    """Resolve every dataset an example names. Returns (staged, problem)."""
    staged = []
    created_here = example_created_files(commands)

    for command in commands:
        m = DATA_REF_RE.match(command)
        if not m:
            continue

        raw = _unquote_path(m.group(1))

        if raw.lower() in created_here or (raw + ".dta").lower() in created_here:
            continue

        if re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.IGNORECASE):
            continue

        name = raw
        if not re.search(r"\.dta$", name, flags=re.IGNORECASE):
            name = name + ".dta"

        found = None

        # Resolution precedence. The parent working directory sits after the
        # sandbox and the help/package source, and before the adopath search
        # below: a file the parent can see is a legitimate input, and the only
        # reason it was missing is that the child runs elsewhere. Section 8
        # requires a resolved input to be STAGED into the child environment,
        # which is what the caller does with the returned pairs.
        for base in (
            ctx.get("sandbox"),
            ctx.get("source_dir"),
            ctx.get("out_dir"),
            ctx.get("parent_pwd"),
        ):
            if not base:
                continue
            candidate = Path(base) / name
            if candidate.exists():
                found = candidate
                break

        if found is None:
            resolved = resolve_source_file(name, ctx.get("roots") or [])
            if resolved is not None and Path(resolved).exists():
                found = Path(resolved)

        if found is None:
            if help_expects_user_data(unit_lines):
                return staged, HelprunError(
                    "USER_DATA_REQUIRED",
                    "helprun: this example is written to run on your own "
                    "dataset; open your data first, then click the example",
                    detail=name,
                )

            # No fuzzy filename substitution: a similarly named file is not
            # the referenced dataset.
            return staged, HelprunError(
                "DATA_FILE_MISSING",
                "helprun: referenced file "
                + name
                + " was not found in the installed package, the help "
                "location, your working directory, earlier setup, or a "
                "download source; the example cannot be reproduced as written",
                detail=name,
            )

        staged.append((name, str(found)))

    return staged, None


# ============================================================
# Child plan construction with behaviour-preserving capture
#
# GATE 2 R13 established that graph creation order cannot be recovered after
# the fact: graph dir is alphabetical and graph describe has one-second
# resolution. Order is therefore observed DURING execution by snapshotting the
# registry after each top-level command. The snapshot is read-only and cannot
# change any statistical result (specification section 13).
#
# GATE 2 also established that graph export by name needs a preceding
# graph save; a bare export by name fails with r(693) for a nodraw graph.
# ============================================================

BLOCK_OPEN_RE = re.compile(
    r"^\s*(mata\s*:|python\s*:|program\s+(define|def)\b|input\b)",
    flags=re.IGNORECASE,
)


def top_level_flags(commands):
    """Which commands are safe injection points (depth 0, outside blocks)."""
    flags = []
    in_block = False
    in_semicolon = False
    depth = 0

    for command in commands:
        s = command.strip()
        low = s.lower()

        if in_block:
            flags.append(False)
            if low == "end":
                in_block = False
            continue

        if BLOCK_OPEN_RE.match(s):
            in_block = True
            flags.append(False)
            continue

        if re.match(r"^#delimit\s*;", low):
            in_semicolon = True
            flags.append(False)
            continue

        if re.match(r"^#delimit\s+cr", low):
            in_semicolon = False
            flags.append(False)
            continue

        opens = s.count("{")
        closes = s.count("}")

        safe = (depth == 0) and (opens == closes) and not in_semicolon
        depth += opens - closes
        if depth < 0:
            depth = 0

        flags.append(safe)

    return flags


# Where the child stores the graphs it snapshots: a directory of this name
# directly beneath the sandbox, which is the child's working directory when the
# preamble runs. Never exported as an authored artifact.
GRAPH_SNAPSHOT_DIR = "_hr_graphs"

# The child resolves that directory ONCE, at preamble time, into an absolute
# path held in a global. An authored `cd` is legitimate and common, and while
# the snapshot path was relative such an example moved the graph directory out
# from under the instrumentation: the postamble's `dir` then named a directory
# that did not exist, and because an extended macro function cannot be silently
# ignored, that aborted an otherwise successful run.
GRAPH_DIR_MACRO = "HR_GDIR"
_GRAPH_DIR = "${" + GRAPH_DIR_MACRO + "}"


def graph_capture_preamble(order_macro="HR_GORDER"):
    """Instrumentation that captures a graph WHEN IT EXISTS, not at the end.

    Two properties this must have, both learned from real failures.

    It is STATE-NEUTRAL. `graph dir` is r-class, so calling it between authored
    commands cleared the r() results the next authored command depended on: the
    real anchor example of HPROD-51 ends `margins` -> `marginsplot` and failed with
    r(301) "previous command was not margins" on every engine that ran it, while
    the identical authored sequence outside helprun completes (HPROD-51). The
    helper therefore holds and restores r() around its own work, so an authored
    command cannot tell that anything ran between it and the next.

    It preserves CONTENT AT SNAPSHOT TIME. Recording only the names and saving
    at the end loses any graph a later authored command drops, replaces or
    invalidates (HPROD-53); the author is not required to write `graph save`.
    Every graph present is saved on every snapshot under a zero-padded sequence,
    so creation order survives and a graph replaced under one name yields two
    files; identical repeats are collapsed by content when they are exported.
    """
    return [
        # The globals survive `clear all` (measured: see the helper below), so
        # the sequence and the snapshot directory persist across anything the
        # example does to its session.
        "global " + order_macro + ' ""',
        "global HR_GSEQ = 0",
        # Bound to the sandbox now, so a later authored `cd` cannot move it.
        "global " + GRAPH_DIR_MACRO + ' "`c(pwd)\'/' + GRAPH_SNAPSHOT_DIR + '"',
        'capture mkdir "' + _GRAPH_DIR + '"',
        # The helper lives in an ado-file in the sandbox and is reached through
        # the adopath, APPENDED so that nothing in the sandbox can shadow a
        # real command. `clear all` drops the loaded copy; Stata then reloads
        # it from disk on the next call, which is the whole point.
        "adopath + \"`c(pwd)'\"",
    ]


def graph_capture_helper_ado(order_macro="HR_GORDER"):
    """The snapshot helper, as an ado-file rather than an inline program.

    An authored `clear all` is ordinary and legitimate -- the real anchor
    example of HPROD-66 opens with it -- and it drops every program in
    memory, including
    this one. The hook that calls it is `capture`d, so nothing was reported:
    the run succeeded and simply produced no graph, for the whole rest of the
    example (HPROD-66).

    Measured in Stata 19.5 (validation/experiments/clear_all.log): `clear all`
    drops programs, but leaves global macros and the adopath intact, and a
    command backed by an ado-file on the adopath is reloaded from disk on its
    next call. So the helper is a file, and its state is in globals. Fighting
    `clear all` with retry logic would have been the alternative; using Stata's
    own on-demand loading needs no retry and cannot fall out of step.
    """
    return [
        "*! helprun graph snapshot helper (internal)",
        "program define _hr_gsnap",
        "    capture _return hold _hr_rstate",
        "    capture quietly graph dir",
        "    if _rc {",
        "        capture _return restore _hr_rstate",
        "        exit",
        "    }",
        '    local now `"`r(list)\'"\'',
        '    local acc `"${' + order_macro + '}"\'',
        "    foreach g of local now {",
        '        local pos : list posof `"`g\'"\' in acc',
        "        if `pos' == 0 {",
        '            local acc `"`acc\' `g\'"\'',
        "        }",
        "    }",
        "    global " + order_macro + ' `"`acc\'"\'',
        "    local _hrseq = ${HR_GSEQ} + 1",
        "    global HR_GSEQ = `_hrseq'",
        "    local _hrtag : display %04.0f `_hrseq'",
        "    foreach g of local now {",
        '        capture quietly graph save `g\' "' + _GRAPH_DIR + '/`_hrtag\'_`g\'.gph", replace',
        "    }",
        "    capture _return restore _hr_rstate",
        "end",
    ]


def render_missing_graph_images(graph_dir):
    """Render a .png beside any saved graph that has none yet.

    The child renders its own graphs in the postamble, which runs after the
    authored region -- and a child that ABORTS never reaches it. So a graph the
    example legitimately produced before a later authored command failed was
    preserved as a .gph the user could open only in Stata, with no image to
    look at (HPROD-64).

    Rendering here, in the parent, after the child has finished, is what makes
    that impossible to get wrong: the authored run is over, so nothing this
    does can disturb it -- unlike exporting inside the snapshot helper, which
    would have had to make a graph current between two authored commands. It
    costs one short Stata only when a run both produced graphs and did not
    reach its own render step.
    """
    directory = Path(graph_dir)

    try:
        missing = sorted(p for p in directory.glob("*.gph")
                         if not p.with_suffix(".png").is_file())
    except OSError:
        return []

    if not missing:
        return []

    try:
        exe = stata_exe()
    except Exception:                                          # noqa: BLE001
        return []

    lines = []
    for gph in missing:
        lines.append('capture quietly graph use "%s"' % gph.name)
        lines.append('capture quietly graph export "%s", replace width(1200)'
                     % gph.with_suffix(".png").name)

    script = directory / "_hr_render.do"

    try:
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run([str(exe), "/e", "do", str(script)], cwd=str(directory),
                       capture_output=True, timeout=180,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    finally:
        for leftover in (script, script.with_suffix(".log")):
            try:
                leftover.unlink()
            except OSError:
                pass

    return [p for p in missing if p.with_suffix(".png").is_file()]


def graph_capture_postamble(out_dir, basename, order_macro="HR_GORDER"):
    """Export a .png beside every graph the snapshots saved.

    GATE 2 established the reliable sequence: load the saved graph, then export
    it. A bare `graph export ..., name()` fails with r(693) "could not find
    Graph window" for a nodraw graph in batch mode. Working from the SAVED
    FILES rather than from live memory is what lets a graph a later authored
    command destroyed still be exported (HPROD-53); `out_dir` and `basename`
    are no longer used here, because the parent names and deduplicates the
    exported pairs once it can compare their content.
    """
    return [
        "capture _hr_gsnap",
        # An extended macro function cannot be told to fail quietly, so the
        # listing is captured: a run that produced no graph, or whose snapshot
        # directory could not be created, must end exactly as it would have
        # without any instrumentation at all.
        'local _hrfiles ""',
        'capture local _hrfiles : dir "' + _GRAPH_DIR + '" files "*.gph"',
        "capture local _hrfiles : list sort _hrfiles",
        "foreach f of local _hrfiles {",
        '    capture quietly graph use "' + _GRAPH_DIR + '/`f\'"',
        '    local _hrstem = subinstr(`"`f\'"\', ".gph", "", 1)',
        '    capture quietly graph export "' + _GRAPH_DIR + '/`_hrstem\'.png", replace width(1200)',
        "}",
    ]


# Markers that fence the authored region of the child plan. Section 12.1A
# requires parent Results to show the authored commands and their ordinary
# Stata results ONCE, with no internal framing or instrumentation. Rather than
# trying to recognise instrumentation by its text -- which would drift the
# moment the instrumentation changed -- the plan states plainly where the
# authored region starts and stops, and the Results transcript keeps only that.
AUTHORED_BEGIN = "HELPRUN-AUTHORED-BEGIN"
AUTHORED_END = "HELPRUN-AUTHORED-END"
GSNAP_CALL = "capture _hr_gsnap"

# Structural provenance. Every line of a child plan belongs to exactly one of
# three classes, and the plan says which rather than leaving it to be guessed
# from the text:
#
#   AUTHORED              a command the help author wrote
#   PREREQUISITE_AUTHORED authored setup an earlier part of the page supplies
#   HELPRUN_INTERNAL      instrumentation helprun injected
#
# Only the first two may reach the user-facing log. Classifying by REGION
# rather than by matching text is what makes an authored `capture drop x`
# survive while `capture _hr_gsnap` does not: the two are textually similar and
# structurally unrelated, so any text rule would have to choose between
# deleting authored code and leaking instrumentation.
INTERNAL_BEGIN = "HELPRUN-INTERNAL-BEGIN"
INTERNAL_END = "HELPRUN-INTERNAL-END"

PROVENANCE_AUTHORED = "AUTHORED"
PROVENANCE_PREREQ = "PREREQUISITE_AUTHORED"
PROVENANCE_INTERNAL = "HELPRUN_INTERNAL"


def build_child_plan(commands, capture, trace_indices=(), worker=None):
    """Interleave read-only capture instrumentation into one child plan.

    Injected instrumentation is fenced by INTERNAL markers so its provenance is
    a structural fact about the plan rather than something a later reader has
    to infer. The per-command graph hook sits inside the authored region by
    necessity -- it must run between authored commands -- so it is fenced
    individually.

    `trace_indices` names the authored commands whose program reads input:
    Stata's trace is switched on immediately before and off immediately after
    each, inside INTERNAL fences, so the parent can see the exact moment the
    program blocks on its request. The trace lines are helprun instrumentation
    and are stripped from every user-facing surface by strip_trace_lines.

    `worker` (a dict with "title" and "log") makes this a plan for the VISIBLE
    interactive worker: unlike a batch child, an interactive Stata neither logs
    by itself nor exits at the end of a do-file, so the plan opens its own log,
    titles its window, and exits Stata when done -- all inside INTERNAL fences.
    """
    trace_indices = set(trace_indices or ())

    def traced(index, command):
        if index not in trace_indices:
            return [command]
        block = ["* " + INTERNAL_BEGIN]
        block.extend(TRACE_ON_COMMANDS)
        block.append("* " + INTERNAL_END)
        block.append(command)
        block.append("* " + INTERNAL_BEGIN)
        block.extend(TRACE_OFF_COMMANDS)
        block.append("* " + INTERNAL_END)
        return block

    out = []

    if not capture:
        body = []
        for index, command in enumerate(commands):
            body.extend(traced(index, command))
        out.extend(["* " + AUTHORED_BEGIN] + body + ["* " + AUTHORED_END])
    else:
        out.append("* " + INTERNAL_BEGIN)
        out.extend(graph_capture_preamble())
        out.append("* " + INTERNAL_END)

        flags = top_level_flags(commands)

        out.append("* " + AUTHORED_BEGIN)

        for index, (command, safe) in enumerate(zip(commands, flags)):
            out.extend(traced(index, command))
            if safe:
                out.append("* " + INTERNAL_BEGIN)
                out.append(GSNAP_CALL)
                out.append("* " + INTERNAL_END)

        out.append("* " + AUTHORED_END)

        out.append("* " + INTERNAL_BEGIN)
        out.extend(
            graph_capture_postamble(capture["out_dir"], capture["basename"])
        )
        out.append("* " + INTERNAL_END)

    return out


WORKER_DONE_MARKER = "DONE.hrworker"
WORKER_GUIDANCE = ("helprun: when this window asks you something, answer in its "
                   "Command box (press Enter to confirm); it closes by itself when "
                   "the example has finished.")


def build_worker_wrapper(worker, authored_name, capture=None):
    """The file the VISIBLE worker is launched with -- through `run`, silently.

    The worker's window is a user-facing surface: the user answers the
    example's prompt there. It must show only the authored commands, their
    output, the prompt, the result and Stata's own errors, plus one line of
    guidance -- never helprun's orchestration (HPROD-47). Stata's `run`
    executes a file without echoing its lines or their output, and a nested
    `noisily do` inside it echoes exactly the nested file: so this wrapper is
    launched with `run`, does its work silently (title, log, graph-capture
    preamble/postamble, completion marker, exit), and executes the authored
    segment -- authored commands only, in `authored_name` -- as one noisy
    nested do-file, whose echo is the same `. command` / output stream a batch
    child produces. No trace is switched on: the pause is detected from the
    log's last echoed command and the process's idleness.

    An interactive Stata neither logs by itself nor exits at the end of a
    file, and after an authored error it stops and stays open, so the wrapper
    opens the log, runs the authored file under `capture noisily do`, writes
    Stata's return code in its standard `r(N);` form when the body failed (the
    line a batch child prints), writes the completion marker only when the
    authored region ran to its end, closes the log and exits Stata. The
    guidance line is displayed before the log opens, so it is seen but is not
    part of the run's transcript.
    """
    title = str(worker.get("title", "helprun worker")).replace('"', "'")
    lines = [
        'window manage maintitle "%s"' % title,
        "set more off",
        'noisily display as text "%s"' % WORKER_GUIDANCE.replace('"', "'"),
        "capture log close hrworker",
        'log using "%s", replace text name(hrworker)' % str(worker.get("log", "plan.log")),
    ]
    if capture:
        # the capture instrumentation runs silently here: its `noisily` was
        # for the batch child's fenced log, and would surface `(file saved)`
        # messages on the worker's user-facing surface
        lines.extend(l.replace("capture noisily ", "capture ") for l in graph_capture_preamble())
    lines.extend([
        'capture noisily do "%s"' % authored_name,
        "local hrworker_rc = _rc",
        "if `hrworker_rc' != 0 {",
        "    noisily display as error \"r(`hrworker_rc');\"",
        "}",
        "else {",
    ])
    if capture:
        lines.extend("    " + l.replace("capture noisily ", "capture ") for l in
                     graph_capture_postamble(capture["out_dir"], capture["basename"]))
    lines.extend([
        "    tempname hrdone",
        '    file open `hrdone\' using "%s", write replace text' % WORKER_DONE_MARKER,
        "    file write `hrdone' \"completed\" _n",
        "    file close `hrdone'",
        "}",
        "capture log close hrworker",
        "exit, clear STATA",
    ])
    return lines


def fence_worker_log(text, completed):
    """Give a worker log the fences the batch child's log carries.

    The worker's log holds the authored region only (the wrapper is silent),
    so the shared transcript filters, which select by fence, need the region
    marked. The nested do-file's own epilogue (`end of do-file` and the blank
    echo before it) is trimmed before the closing fence; an unfinished run --
    an authored error, or a window the user closed -- stays unclosed, exactly
    like an aborted batch child, so the same epilogue rule applies to it.
    """
    lines = text.splitlines()
    if completed:
        while lines and (lines[-1].strip() in ("", ".") or _BATCH_EPILOGUE_RE.match(lines[-1].strip())):
            lines.pop()
    out = [". * " + AUTHORED_BEGIN] + lines
    if completed:
        out.append(". * " + AUTHORED_END)
    return chr(10).join(out) + chr(10)


# Stata's own batch epilogue, which follows the authored region when a
# child aborts. Anchored, so an authored command that merely mentions the
# phrase cannot truncate the transcript.
_BATCH_EPILOGUE_RE = re.compile(r'^(?:end of do-file|r\([0-9]+\);)$')


def user_facing_transcript(child_log):
    """The child transcript with every HELPRUN_INTERNAL region removed.

    WHY THIS IS SHARED WITH THE RESULTS BRIDGE

    The persistent log used to embed the RAW child transcript verbatim, under a
    `CHILD OUTPUT` heading, while only parent Results was filtered. The
    docstring of the Results filter said so plainly -- "Everything stripped
    here is still preserved in full in the persistent log" -- and that is
    exactly what shipped: users opening topic-example-N.log found
    HELPRUN-AUTHORED markers, the _hr_gsnap program definition, its
    capture/program drop/global/foreach instrumentation, graph save/use/export
    calls, sandbox and TEMP paths, and Stata's wrapper epilogue.

    The clean-output validation checked parent Results and never opened the
    log, so it passed throughout. One filter now serves both surfaces, which is
    what stops them diverging again.

    Filtering is by REGION, never by text. An authored `capture drop x` is
    inside the authored region and survives; `capture _hr_gsnap` is inside an
    internal region and does not.
    """
    if not child_log:
        return ""

    lines = strip_trace_lines(child_log).splitlines()

    # A child that ABORTS never reaches AUTHORED_END: Stata stops at the
    # failing command, prints its epilogue -- `end of do-file` and the final
    # `r(NNN);` -- and exits. The epilogue is then inside the still-open
    # authored region, and the first shared-filter version let it through on
    # every failed run (A58, I26, and the GATE 5 baseline all caught it). The
    # authored error message itself is kept: it is the meaningful Stata error
    # the user needs. Only when the region never closes is the epilogue cut,
    # so an authored `capture` that prints an r() code mid-example and carries
    # on is untouched.
    region_closed = any(AUTHORED_END in (l.strip()[1:].strip()
                                         if l.strip().startswith(".")
                                         else l.strip())
                        for l in lines)

    out = []
    depth = 0
    seen_authored = False
    in_authored = False

    for raw in lines:
        stripped = raw.strip()
        bare = stripped[1:].strip() if stripped.startswith(".") else stripped

        if (in_authored and not region_closed and depth == 0
                and _BATCH_EPILOGUE_RE.match(bare)):
            # the child aborted here; everything after is batch scaffolding
            break

        if INTERNAL_BEGIN in bare:
            depth += 1
            continue
        if INTERNAL_END in bare:
            depth = max(0, depth - 1)
            continue
        if depth:
            continue

        if AUTHORED_BEGIN in bare:
            seen_authored = True
            in_authored = True
            continue
        if AUTHORED_END in bare:
            in_authored = False
            continue

        if not seen_authored:
            # Anything before the authored region is Stata's batch banner and
            # helprun's own scaffolding.
            continue

        if not in_authored:
            # Past the authored region: only the batch epilogue remains, and
            # the ado prints the causal message and its code itself.
            if _BATCH_EPILOGUE_RE.match(bare):
                break
            continue

        # A bare echo of the graph hook, for a plan built before the internal
        # fences existed or if a fence is ever lost.
        if bare == GSNAP_CALL:
            continue

        out.append("" if stripped in (".", "") else raw)

    cleaned = []
    for line in out:
        if line == "" and cleaned and cleaned[-1] == "":
            continue
        cleaned.append(line)

    return chr(10).join(cleaned).strip(chr(10))


def results_transcript(child_log):
    """The authored region of a child log, fit for parent Results.

    WHY THIS IS NOW A ONE-LINE DELEGATION

    It used to be its own filter, stripping the AUTHORED markers and the graph
    hook. When the persistent log gained a second, region-aware filter, the two
    surfaces became two implementations of the same rule -- and they diverged
    on the first change: the INTERNAL fences added for the log leaked straight
    into Results, and a human watching a real click saw
    `* HELPRUN-INTERNAL-BEGIN` echoed throughout. The report that one filter
    already served both surfaces was wrong; only the log used it.

    Section 12.1A wants Results to resemble ordinary interactive Stata: no
    batch banner, no run-log header, no instrumentation, no duplicate streams.
    That is exactly the property user_facing_transcript enforces, so this
    function no longer has its own opinion about what to strip.
    """
    return user_facing_transcript(child_log)


STATA_NOISE = (
    "end of do-file",
    "r(0);",
)


def causal_stata_error(child_log):
    """The strongest evidence-backed Stata error, with its return code.

    WHY THIS IS NOT A BACKWARD WALK FROM THE LAST r()

    Stata's batch epilogue ends every failed run with `end of do-file` and a
    final `r(NNN);`. The previous implementation started at that last `r()` and
    walked back twelve lines for a message. Between the authored failure and
    the epilogue sit helprun's own graph-capture postamble and Stata's echo of
    it, so the window closed on nothing but echoes and the function returned an
    empty message with a bare code. The caller then reported `end of do-file`,
    or fell back to AMBIGUOUS_FAILURE_PROVENANCE, while the real cause --
    `invalid 'nine'`, `is not a valid command name`, `file not found` -- sat
    plainly in the log a little further up.

    So the FIRST genuine failure is preferred instead of the last, because the
    first is causal and everything after it is consequence, and the search is
    not bounded by a fixed window that instrumentation can overflow. Internal
    regions are skipped outright: a failure inside helprun's own instrumentation
    is not the example's error and must never be reported as though it were.

    AMBIGUOUS_FAILURE_PROVENANCE remains available to the caller, but only for
    what it means: evidence that genuinely cannot attribute responsibility.
    """
    if not child_log:
        return "", ""

    lines = [l.rstrip() for l in child_log.splitlines()]

    # Mark the internal regions so their content cannot be mistaken for the
    # example's own failure.
    internal = [False] * len(lines)
    depth = 0
    for i, line in enumerate(lines):
        bare = line.strip()
        if bare.startswith("."):
            bare = bare[1:].strip()
        if INTERNAL_BEGIN in bare:
            depth += 1
            internal[i] = True
            continue
        if INTERNAL_END in bare:
            internal[i] = True
            depth = max(0, depth - 1)
            continue
        internal[i] = depth > 0

    def message_before(index):
        """The nearest real message above a return code."""
        for j in range(index - 1, -1, -1):
            if internal[j]:
                continue
            candidate = lines[j].strip()
            if not candidate:
                continue
            if candidate.startswith(". ") or candidate.startswith("> "):
                # an echoed command: the failure text, if any, is above it
                continue
            if candidate in STATA_NOISE:
                continue
            if re.match(r"^r\(\d+\);$", candidate):
                # reached the previous failure; this one has no message
                return ""
            if AUTHORED_BEGIN in candidate or AUTHORED_END in candidate:
                return ""
            return candidate
        return ""

    fallback_code = ""

    for i, line in enumerate(lines):
        m = re.match(r"^r\((\d+)\);\s*$", line.strip())
        if not m or internal[i]:
            continue

        code = m.group(1)
        if code == "0":
            continue

        if not fallback_code:
            fallback_code = code

        message = message_before(i)
        if message:
            return message, code

    return "", fallback_code


# ============================================================
# Authored artifact preservation
# ============================================================

AUTHORED_ARTIFACT_EXTENSIONS = {
    ".dta", ".csv", ".xlsx", ".xls", ".docx", ".pdf", ".tex",
    ".html", ".htm", ".svg", ".txt", ".rtf", ".png", ".gph",
}

HELPRUN_INTERNAL_NAMES = {
    "plan.do", "plan.log", "plan_1.do", "plan_1.log",
    "plan_2.do", "plan_2.log", "plan_3.do", "plan_3.log",
    "plan_4.do", "plan_4.log",
    # the graph snapshot helper, which lives in the sandbox as an ado-file so
    # that an authored `clear all` cannot take it away (HPROD-66)
    "_hr_gsnap.ado",
}


# Directories beneath the private TEMP that hold runtime machinery, never results:
# caches, package/runtime installs, compiled or intermediate files.
_RUNTIME_DIR_RE = re.compile(
    r"^(?:.*cache.*|__pycache__|node_modules|jar|jars|lib|libs|pip|npm|hsperfdata.*|"
    r"\.matplotlib|\.ipython|\.jupyter|site-packages|classes|build|dist|tmp\d*)$",
    re.IGNORECASE)
# Stata's own temporary files and generic scratch names.
_SCRATCH_NAME_RE = re.compile(r"^(?:ST_[0-9a-z]+(?:\.\w+)?|.*\.tmp|~.*|\..*)$", re.IGNORECASE)


def _substantive_html(path):
    """An HTML file that is a result page, not a redirect/loader stub or a
    fragment: it has a document body with visible text, and is not a page whose
    only purpose is to send the browser elsewhere (specification 12.3)."""
    try:
        if path.stat().st_size < 512:
            return False
        head = path.read_bytes()[:400000].decode("utf-8", errors="replace")
    except OSError:
        return False
    low = head.lower()
    if "<body" not in low and "<svg" not in low:
        return False
    visible = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", head, flags=re.S | re.I)
    visible = re.sub(r"\s+", " ", visible).strip()
    if re.search(r"http-equiv\s*=\s*[\"']?refresh", low) and len(visible) < 200:
        # a refresh page is a stub unless it also carries real content
        return False
    return len(visible) >= 80


def collect_authored_artifacts(sandbox, pre_existing):
    """Files the example, or a runtime it invoked, created as FINAL artifacts.

    Classification is by nature and attribution, never by location or by
    topic (specification 12.3, HPROD-49): a file beneath the run's private
    TEMP -- where Java/Python/R/JavaScript runtimes legitimately write their
    results -- is a final artifact when it carries an authored-output format,
    is non-empty, is not a cache/runtime/scratch/intermediate file and (for
    HTML) is a substantive document. Temporary caches, runtime files and
    helprun's own plan/log files are never exported.

    Returns [(path, normalize)] in creation order: `normalize` is True for a
    file whose name the runtime chose as a temporary under TEMP -- it is
    preserved under the run's identity name -- and False for a file the
    example wrote by an authored name, which keeps that name.
    """
    found = []
    sandbox = Path(sandbox)

    for path in sorted(sandbox.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(sandbox)

        if rel.parts and rel.parts[0] in ("_hr_out", GRAPH_SNAPSHOT_DIR):
            continue
        if path.name in HELPRUN_INTERNAL_NAMES:
            continue
        if path.suffix.lower() not in AUTHORED_ARTIFACT_EXTENSIONS:
            continue
        if str(rel).lower() in pre_existing:
            continue

        in_temp = bool(rel.parts) and rel.parts[0] == "_tmp"
        if in_temp:
            # nature, not location: runtime machinery under TEMP is excluded
            if any(_RUNTIME_DIR_RE.match(part) for part in rel.parts[1:-1]):
                continue
            if _SCRATCH_NAME_RE.match(path.name):
                continue
            try:
                if path.stat().st_size == 0:
                    continue
            except OSError:
                continue
            if path.suffix.lower() in (".html", ".htm") and not _substantive_html(path):
                continue

        found.append((path, in_temp))

    # creation order for the runtime-named artifacts; authored names keep the
    # existing sorted order after them
    def order(item):
        p, normalize = item
        try:
            return (0 if normalize else 1, p.stat().st_mtime if normalize else 0, str(p).lower())
        except OSError:
            return (1, 0, str(p).lower())

    return sorted(found, key=order)


# ============================================================
# Public runtime entry
#
# Public UX is frozen (specification section 2.1):
#     help xxx
#     helprun
# then the user clicks a visible "Run this example" link. There is no public
# example number, topic or file option. The hrclick() token below is an
# internal, undocumented, source-bound identity produced only by helprun's own
# generated links; it is not a selection mechanism a user can meaningfully
# type, and it is never documented as public API.
# ============================================================

HELPRUN_VERSION = "1.0.0"


class UnitList(list):
    """A unit list that also carries the units it had to skip, and why."""

    __slots__ = ("skipped",)

    def __init__(self, items=()):
        super().__init__(items)
        self.skipped = []


def runnable_units_for(source, roots):
    """Every structural Example of one source that reconstructs to real code.

    A unit whose reconstruction is genuinely ambiguous is skipped rather than
    allowed to abort the whole help page: one unrunnable example must not make
    every other example on the page unavailable. The skip is recorded on the
    returned list so callers can report it.
    """
    units = []
    skipped = []

    for raw_unit in extract_units(source, roots):
        authored = []
        try:
            code = reconstruct_unit(source, raw_unit, roots, source_out=authored)
        except HelprunError as exc:
            skipped.append(
                {"heading": raw_unit.get("heading", ""), "reason": exc.reason}
            )
            continue

        if not code:
            continue

        unit = dict(raw_unit)
        unit["code"] = list(code)
        # The author's own lines, kept beside the derived commands so the code
        # record can show both without presenting either as the other.
        unit["authored_source"] = list(authored)
        # Carried so click_run can refuse a unit that ends inside an open
        # block, without that refusal costing the unit its place on the page.
        unit["open_block"] = getattr(code, "open_block", None)
        # Where the Run control belongs: immediately after the example's final
        # executable command, not at the unit's structural end. A trailing
        # non-runnable section -- video list, notes, references, a native-link
        # collection, plain prose -- sits inside the range but after everything
        # that runs, and a control placed past it reads as belonging to that
        # section rather than to the example.
        unit["last_command_line"] = getattr(code, "last_command_line", None)
        # Fragments the reconstruction could not justify. Carried onto the unit
        # so click_run can refuse with a clear reason instead of feeding Stata
        # a line that is not a command; `unit["code"]` is a plain list copy, so
        # without this the finding would be dropped between here and the click.
        unit["unreliable_fragments"] = list(
            getattr(code, "unreliable_fragments", []))
        units.append(unit)

    for index, unit in enumerate(units, start=1):
        unit["ordinal"] = index

    result = UnitList(units)
    result.skipped = skipped
    return result


def locate_runnable_document(root_topic, root_source, roots, max_depth=3):
    """Follow authored help links until a source with runnable examples is found.

    Delegation is followed through links the author actually wrote, and every
    hop is resolved by Stata (specification section 4). No similarly named help
    file is ever guessed at.
    """
    queue = [(root_topic, root_source, 0)]
    visited = set()

    while queue:
        topic, source, depth = queue.pop(0)

        key = str(source).lower()
        if key in visited:
            continue
        visited.add(key)

        units = runnable_units_for(source, roots)

        if units:
            return {
                "topic": topic,
                "source": source,
                "units": units,
                "depth": depth,
            }

        if depth >= max_depth:
            continue

        for _line_no, linked, _raw in help_links(source, roots):
            linked_source = resolve_help_topic(linked, roots)
            if linked_source is not None:
                queue.append((linked, linked_source, depth + 1))

    return None


def resolve_topic_document(topic, stata_roots=None):
    """Resolve a topic to its runnable document, source graph and units."""
    exe = stata_exe()
    roots = ado_roots(exe, stata_roots)

    root_source = resolve_help_topic(topic, roots)

    if root_source is None:
        raise HelprunError(
            "PACKAGE_FILE_MISSING",
            "helprun: no help source could be resolved for " + str(topic),
        )

    doc = locate_runnable_document(topic, root_source, roots)

    if doc is None:
        raise HelprunError(
            "NO_RUNNABLE_EXAMPLE",
            "helprun: this help page has no runnable example",
            detail=str(root_source),
        )

    graph = build_source_graph(doc["source"], roots)

    return {
        "root_topic": topic,
        "root_source": root_source,
        "source": doc["source"],
        "graph": graph,
        "units": doc["units"],
        "roots": roots,
        "exe": exe,
    }


def _run_link_line(handle):
    """The Viewer's Run control.

    Stata echoes a clicked {stata ...} link verbatim, so whatever this line
    contains is what the user sees in Results. It therefore carries a short
    content-addressed handle, not the identity payload itself.
    """
    return (
        "{p 8 8 2}({stata helprun, hrclick("
        + handle
        + "):Run this example}){p_end}"
    )


def prepare_clickable(stata_roots=None, topic_override=None):
    """Bare helprun: preparation only.

    This performs exactly the work needed to identify, read, parse and snapshot
    the active help and build the temporary clickable Viewer. It executes no
    example, launches no child Stata, downloads nothing and creates no
    example-N learning artifacts (specification section 2).
    """
    cleanup_stale_views()

    if topic_override:
        topic = str(topic_override).strip()
    else:
        topic = current_viewer()["topic"]

    doc = resolve_topic_document(topic, stata_roots)

    graph = doc["graph"]
    units = doc["units"]
    lines = graph.lines

    insertion_after = {}

    # The identity payload is filed in the private click registry and the link
    # carries only its content-addressed handle, so a click echoes a short
    # readable command instead of a 200-character token.
    prune_click_registry()

    unplaceable = []

    for unit in units:
        # The control is anchored to the last executable command. If that line
        # is unknown, or falls outside the unit, the placement is ambiguous and
        # the specification requires OMITTING the control and recording the
        # example as unresolved -- never guessing a position.
        anchor_line = unit.get("last_command_line")

        # The control must also fall BEFORE any subsequent structural section.
        # A trailing video list, notes, references or native-link collection can
        # sit inside the unit range; a control placed past one reads as
        # belonging to that section rather than to the example. Clamping to the
        # first such boundary is a general rule about document structure and
        # names no topic.
        boundary = None
        for probe in range(unit["start"] + 1, unit["end"] + 1):
            if peer_structural_boundary(lines[probe - 1]):
                boundary = probe
                break

        if boundary is not None and anchor_line is not None                 and anchor_line >= boundary:
            anchor_line = None

        if (anchor_line is None
                or not (unit["start"] <= anchor_line <= unit["end"])):
            unplaceable.append({
                "ordinal": unit.get("ordinal"),
                "heading": unit.get("heading", ""),
                "reason": "AMBIGUOUS_RUN_CONTROL_PLACEMENT",
            })
            continue

        identity = build_click_identity(topic, doc["source"], graph, unit)
        handle = store_click_identity(identity)
        insertion_after.setdefault(anchor_line, []).append(handle)

    out = []
    link_count = 0

    for line_no, raw in enumerate(lines, start=1):
        # The author's visible content is preserved verbatim, in order, with
        # native {stata ...} links untouched.
        out.append(str(raw))

        for handle in insertion_after.get(line_no, []):
            out.extend(["", _run_link_line(handle), ""])
            link_count += 1

    if link_count != len(units) - len(unplaceable):
        raise HelprunError(
            "HELPRUN_INTERNAL_ERROR",
            "helprun: click-link generation did not match the example count",
        )

    view_dir = Path(tempfile.mkdtemp(prefix=VIEW_DIR_PREFIX))

    viewfile = view_dir / (
        "helprun_" + safe_basename(topic).replace(" ", "_") + ".sthlp"
    )

    viewfile.write_text("\n".join(out) + "\n", encoding="utf-8")

    return {
        "status": "VIEW",
        "topic": topic,
        "source": str(doc["source"]),
        "viewfile": str(viewfile),
        "n_examples": len(units),
        "n_links": link_count,
        "source_files": len(graph.files),
        "aggregate_hash": graph.aggregate_hash,
    }


def prepare_public(stata_roots=None, topic_override=None):
    try:
        result = prepare_clickable(stata_roots, topic_override)
        result.update({"ok": True, "reason": "", "failure_class": "", "error": ""})
        return result

    except HelprunError as exc:
        return {
            "ok": False,
            "status": STATUS_REFUSED,
            "reason": exc.reason,
            "failure_class": exc.failure_class,
            "topic": "",
            "source": "",
            "viewfile": "",
            "n_examples": 0,
            "n_links": 0,
            "error": exc.message,
        }

    except Exception as exc:
        return {
            "ok": False,
            "status": STATUS_FAILED,
            "reason": "HELPRUN_INTERNAL_ERROR",
            "failure_class": CLASS_INTERNAL,
            "topic": "",
            "source": "",
            "viewfile": "",
            "n_examples": 0,
            "n_links": 0,
            "error": "helprun: " + str(exc),
        }


def _write_run_log(out_dir, basename, sections):
    log_path = Path(out_dir) / (basename + ".log")
    log_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return log_path


_ENGINE_IDENTITY = None


def engine_identity():
    """SHA-256 of this engine file, computed once."""
    global _ENGINE_IDENTITY
    if _ENGINE_IDENTITY is None:
        try:
            _ENGINE_IDENTITY = hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest()
        except (OSError, NameError):
            _ENGINE_IDENTITY = "unknown"
    return _ENGINE_IDENTITY


# ============================================================
# Persistent example code artifact, standalone do-file, run manifest
#
# Scope decision SCOPE-002 (validation/scope_decisions.md): promoted into
# HELPRUN 1.0 from the post-1.0 backlog, as a NEW FEATURE rather than a defect
# correction. The run log is the runtime record. These are the CODE and
# PROVENANCE record, and they exist independently of it, so that a user can see
# what the author wrote, what helprun added, what actually ran and where it
# stopped without reading a transcript.
#
# Three rules shape everything below.
#
#   Authored code is never merged with helprun's additions. They are separate
#   sections with separate names, and "None" is written out when helprun added
#   nothing, because an absent section and an empty one read the same.
#
#   The complete authored code is preserved even when execution stopped early.
#   Saving only the part that ran would turn the record of a failure into a
#   shorter, apparently successful example.
#
#   The execution boundary comes from execution evidence. A command that was
#   reconstructed but never echoed was never attempted, and is reported as not
#   executed -- not as failed, and not as run.
# ============================================================

CODE_ARTIFACT_SUFFIX = "-code.txt"
MANIFEST_SUFFIX = "-manifest.json"
DO_ARTIFACT_SUFFIX = ".do"

_ECHO_BLOCK_BODY_RE = re.compile(r"^\s+\d+\.\s(.*)$")

# A runtime that is not Stata, so a Stata do-file cannot faithfully stand in
# for the example. `python script` is already in EXTERNAL_LAUNCH_RE; a `python:`
# block and an inline `python ... end` region are the same problem.
_OTHER_RUNTIME_RE = re.compile(r"^\s*(?:python\b|java\b|javacall\b)", flags=re.IGNORECASE)


def _canon_echo(text):
    """Whitespace-insensitive form of a command.

    Stata wraps an echo longer than linesize with a `> ` prefix and splits it
    mid-token, so a comparison that respects spacing cannot match a long
    authored command against its own echo.
    """
    return re.sub(r"\s+", "", str(text).strip())


def transcript_echoes(transcript):
    """The commands Stata echoed, in order.

    Three shapes appear in a log: a top-level command as `. command`; the body
    of a block as numbered lines `  2. command`, the closing brace included;
    and a wrapped continuation as `> ...`, which belongs to the line above only
    when that line was itself part of a command.
    """
    echoes = []
    open_command = False

    for raw in (transcript or "").splitlines():
        line = raw.rstrip()

        if line.startswith(". "):
            echoes.append(line[2:])
            open_command = True
            continue

        body = _ECHO_BLOCK_BODY_RE.match(line)

        if body:
            echoes.append(body.group(1))
            open_command = True
            continue

        if open_command and line.startswith("> "):
            echoes[-1] = echoes[-1] + line[2:]
            continue

        open_command = False

    return echoes


def executed_boundary(commands, transcript):
    """Where execution actually reached, read from the child's own transcript.

    The authored commands are matched, in authored order, against what Stata
    echoed. The first one that never appears ends the attempted run; it and
    everything after it are the unexecuted remainder. Nothing here concludes
    that an attempted command succeeded or that an unexecuted one would have
    failed -- both are inferences the evidence does not support.
    """
    wanted = list(commands or [])
    echoes = [_canon_echo(e) for e in transcript_echoes(transcript)]

    # Membership, not a moving cursor. A block command -- `program define ...
    # end`, a foreach, an input -- is ONE authored command whose lines Stata
    # echoes separately, at DEFINITION time, and the body then runs later when
    # the program is called. Matching in strict order against a single advancing
    # cursor therefore consumed echoes out of step and reported commands as
    # unexecuted that the transcript plainly showed. What the evidence actually
    # supports is narrower and safer: a command Stata echoed was attempted, and
    # one it never echoed was not. Order is taken from the AUTHOR, which is
    # where it is known exactly.
    seen = set(echoes)

    def echoed(command):
        text = str(command)
        first = text.splitlines()[0] if "\n" in text else text
        return _canon_echo(text) in seen or _canon_echo(first) in seen

    attempted = [c for c in wanted if echoed(c)]
    remaining = [c for c in wanted if not echoed(c)]

    return {
        "authored_total": len(wanted),
        "attempted": attempted,
        "attempted_count": len(attempted),
        "executed_through": attempted[-1] if attempted else "",
        "first_not_executed": remaining[0] if remaining else "",
        "stopped_before": remaining,
        "complete": bool(wanted) and not remaining,
        "started": bool(attempted),
    }


def prerequisite_units(plan, target):
    """The units helprun added to the plan; the clicked one is not one of them."""
    return [u for u in (plan or []) if u is not target]


def authored_boundary(target, plan, transcript):
    """The execution boundary expressed over the AUTHORED commands alone.

    The child runs helprun's prerequisites before the clicked example, so the
    match is anchored over the whole executed sequence -- otherwise a command
    the author repeats after a prerequisite would match the prerequisite's echo
    -- and only then narrowed to the author's own commands. The clicked unit is
    last in the plan, which is what makes the narrowing a suffix.
    """
    authored = [str(c) for c in ((target or {}).get("code") or [])]

    full = []
    for unit in (plan or []):
        full.extend(str(c) for c in (unit.get("code") or []))

    if not full:
        full = list(authored)

    # The authored half is judged on its own. An earlier version narrowed a
    # whole-plan result by slicing off a prerequisite-sized prefix, which was
    # only ever valid while matching was a strict in-order walk: once a command
    # Stata never echoed could sit anywhere in the list, the count of attempted
    # commands stopped being an index into the authored ones, and the record
    # named the wrong command as the first not executed.
    own = executed_boundary(authored, transcript)
    whole = executed_boundary(full, transcript)

    own["prerequisites_attempted"] = max(
        whole["attempted_count"] - own["attempted_count"], 0)
    return own


def do_representation_blocker(target, plan, units, interactive=None,
                              staged_inputs=None):
    """Why a faithful standalone do-file cannot be written, or "" when it can.

    A .do is written only where a faithful standalone Stata representation is
    PROVABLE. Each test below names one way that proof fails, and each is
    structural: no help topic, package or command name takes part in it. When
    the proof fails the .txt record is still written -- the provenance record
    does not depend on the example being reducible to a do-file.
    """
    if not target or not target.get("code"):
        return "the authored code was not reconstructed"

    if any(u.get("open_block") for u in (plan or [])):
        return "an authored block is left open in the help source"

    commands = []
    for unit in (plan or []):
        commands.extend(unit.get("code") or [])

    if interactive and interactive.get("pauses"):
        return ("the example asks the user a question, and the answers a person "
                "typed are not part of the authored code")

    if any(EXTERNAL_LAUNCH_RE.match(str(c)) for c in commands):
        return "the example launches a program outside Stata"

    if any(_OTHER_RUNTIME_RE.match(str(c)) for c in commands):
        return "the example runs code in another language inside Stata"

    if staged_inputs:
        return "the example runs against input files helprun staged for it"

    if continues_earlier_example(target, units) and not prerequisite_units(plan, target):
        return ("the example continues from an earlier example in the same help "
                "topic, whose state a standalone file cannot supply")

    return ""


def do_artifact_lines(target, plan):
    """The standalone do-file: executable Stata code, and nothing else.

    This SERIALIZES the reconstructed commands. Where the help page's own
    structure proved that several authored fragments are one command, that
    command is written on one line, which is legal Stata and needs no
    continuation device. helprun does not introduce `///` or `#delimit` for
    convenience, and it never repairs authored semantics to make a file run:
    an example whose faithful serialization cannot be established gets no .do
    at all, and the reason is recorded in the code record and the manifest.

    Where helprun added prerequisites the file must still say which lines are
    the author's and which are helprun's, because that distinction may never be
    lost. One marker comment per section carries it; nothing else is written --
    no prose, no status, no hashes, no manifest fields. An example that needed
    no prerequisites gets the authored commands alone, with no markers at all.
    """
    prereqs = prerequisite_units(plan, target)
    lines = []

    if prereqs:
        for unit in prereqs:
            lines.append("* helprun-added prerequisite: example %s"
                         % unit.get("ordinal", ""))
            lines.extend(str(c) for c in (unit.get("code") or []))
        lines.append("* authored help code: example %s" % target.get("ordinal", ""))

    lines.extend(str(c) for c in (target.get("code") or []))
    return lines


def code_artifact_sections(identity, target, plan, boundary, status,
                           reason="", message="", do_note=""):
    """The persistent .txt code and provenance record for one clicked run."""
    prereqs = prerequisite_units(plan, target)

    lines = [
        "helprun " + HELPRUN_VERSION + " example code record",
        "=" * 60,
        "topic            : " + str(identity.get("topic", "")),
        "example ordinal  : " + str(identity.get("ord", "")),
        "example heading  : " + str((target or {}).get("heading", "")),
        "root source      : " + str(identity.get("root", "")),
        "source graph hash: " + str(identity.get("agg", "")),
        "helprun version  : " + HELPRUN_VERSION,
        "engine sha256    : " + engine_identity(),
        "=" * 60,
        "",
        # THE AUTHOR'S OWN LINES. Never helprun's serialization of them: a
        # `///`, a `#delimit` or a join that helprun introduced to make a
        # standalone file legal is helprun's, and showing it here would rewrite
        # what the author wrote.
        "AUTHORED HELP CODE",
        "-" * 60,
    ]

    source = [str(c) for c in ((target or {}).get("authored_source") or [])]
    commands = [str(c) for c in ((target or {}).get("code") or [])]

    lines.extend(redact_secrets(c) for c in (source or commands))

    # What helprun executes. It differs from the lines above only where the
    # authored SMCL structure PROVED that several source fragments are one
    # command -- an open paragraph continued by {break}, a fragment carrying no
    # `. ` prompt, a syntactically incomplete accumulated command. Where the
    # two are the same, saying so is shorter and clearer than repeating them.
    lines.extend(["", "COMMANDS HELPRUN RECONSTRUCTED", "-" * 60])

    if not source or source == commands:
        lines.append("The same lines, unchanged: no source fragment needed joining.")
    else:
        lines.append("%d authored line(s) resolve to %d command(s), joined only where "
                     "the help page's own structure proved they are one command:"
                     % (len(source), len(commands)))
        lines.append("")
        lines.extend(redact_secrets(c) for c in commands)

    lines.extend(["", "HELPRUN-ADDED PREREQUISITES", "-" * 60])

    if prereqs:
        for unit in prereqs:
            lines.append("from example %s (%s):"
                         % (unit.get("ordinal", ""), unit.get("heading", "")))
            lines.extend("    " + redact_secrets(str(c))
                         for c in (unit.get("code") or []))
    else:
        lines.append("None")

    lines.extend(["", "EXECUTION", "-" * 60])

    # "Reached" rather than "succeeded": the transcript proves which commands
    # Stata ran, and the last one it ran is very often the one that failed.
    # Reporting it as executed successfully would read the evidence for more
    # than it says.
    if not boundary or not boundary.get("started"):
        lines.append("Execution did not start; no authored command was run.")
    elif boundary.get("complete") and status == STATUS_SUCCESS:
        lines.append("Every authored command executed, through:")
        lines.append("    " + redact_secrets(boundary.get("executed_through", "")))
    elif boundary.get("complete"):
        lines.append("Every authored command was reached; the last was:")
        lines.append("    " + redact_secrets(boundary.get("executed_through", "")))
    else:
        lines.append("Executed through:")
        lines.append("    " + redact_secrets(boundary.get("executed_through", "")))
        lines.append("")
        lines.append("Stopped before:")
        lines.extend("    " + redact_secrets(str(c))
                     for c in boundary.get("stopped_before", []))

    if status != STATUS_SUCCESS:
        lines.extend(["", "STOP REASON", "-" * 60])
        lines.append(reason or "not classified")

        if message:
            lines.append(redact_secrets(message))

    if do_note:
        lines.extend(["", "STANDALONE DO-FILE", "-" * 60, do_note])

    return lines


def do_artifact_note(blocker, name=""):
    """The one line the code record and the manifest both carry about the .do."""
    if blocker:
        return "Not written: " + blocker + ". The authored code above is the record."
    return "Written as " + name + "."


def _artifact_integrity(path):
    """SHA-256 and size of a file that has reached its final persisted state."""
    p = Path(path)

    try:
        data = p.read_bytes()
    except OSError:
        return None

    return {
        "name": p.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def run_manifest(identity, target, plan, boundary, status, reason, message,
                 out_dir, files, do_blocker=""):
    """The machine-readable index of one clicked run.

    `files` are the run's persistent files, already in their final state: the
    manifest records each one's size and SHA-256 as read back from disk, so a
    hash is never claimed for a state that was written later. Paths are
    relative to the run's own directory so the record travels with it; the
    authoritative help source keeps its absolute path, because that is what the
    provenance means.

    Nothing internal is indexed here. The manifest names the user's own files
    and the identity of the run that produced them.
    """
    directory = Path(out_dir)
    indexed = []

    for path in files:
        if not path:
            continue

        info = _artifact_integrity(path)

        if info is None:
            continue

        try:
            info["path"] = Path(path).relative_to(directory).as_posix()
        except ValueError:
            info["path"] = Path(path).name

        info["verified"] = True
        indexed.append(info)

    return {
        "helprun_version": HELPRUN_VERSION,
        "engine_sha256": engine_identity(),
        "topic": str(identity.get("topic", "")),
        "ordinal": identity.get("ord", ""),
        "heading": str((target or {}).get("heading", "")),
        "help_source": str(identity.get("root", "")),
        "help_source_hash": str(identity.get("agg", "")),
        "run_id": str(identity.get("topic", "")) + "-" + str(identity.get("ord", "")),
        "status": status,
        "reason": reason or "",
        "message": redact_secrets(message or ""),
        "prerequisites": [u.get("ordinal") for u in prerequisite_units(plan, target)],
        # Why no standalone do-file was written, when none was. The reason
        # belongs in the machine-readable record too, not only in the prose
        # one, so a reader that indexes runs can tell a deliberate omission
        # from a missing file.
        "do_artifact_omitted": do_blocker or "",
        "execution": {
            "authored_commands": (boundary or {}).get("authored_total", 0),
            "attempted": (boundary or {}).get("attempted_count", 0),
            "executed_through": redact_secrets((boundary or {}).get("executed_through", "")),
            "first_not_executed": redact_secrets((boundary or {}).get("first_not_executed", "")),
            "complete": bool((boundary or {}).get("complete")),
        },
        "files": indexed,
    }


def _log_header(identity, target, plan, extra=()):
    lines = [
        "helprun " + HELPRUN_VERSION + " run log",
        "=" * 60,
        "topic            : " + str(identity.get("topic", "")),
        "example ordinal  : " + str(identity.get("ord", "")),
        "example heading  : " + str(target.get("heading", "")) if target else "",
        "root source      : " + str(identity.get("root", "")),
        "source files     : " + str(identity.get("n", "")),
        "source graph hash: " + str(identity.get("agg", "")),
        "prerequisites    : "
        + (
            ", ".join(str(u["ordinal"]) for u in plan if u is not target)
            or "none"
        ),
        "helprun version  : " + HELPRUN_VERSION,
        # Disclosure, always, success or failure (specification 10, HPROD-59).
        # The example ran in its own Stata session, so nothing it left in
        # memory reaches the session the user clicked from. Saying so once, in
        # the log the user opens, is the V1 answer: helprun does not propagate
        # worker state back into the parent and does not share an execution
        # context, and a user who is not told that will read an empty parent
        # session as a helprun failure.
        "session          : ran in a separate Stata session; data, estimation "
        "results and settings it created are not loaded in your session",
        # The engine identity, so a log says which build wrote it. Without it a
        # validation scan cannot tell a log produced by the current engine from
        # one left behind by an earlier build, and a corpus assertion over a
        # directory of accumulated logs is either vacuous or permanently red.
        "engine sha256    : " + engine_identity(),
    ]
    lines.extend(extra)
    lines.append("=" * 60)
    return lines


def click_run(token, parent_pwd=None, stata_roots=None, timeout_seconds=90, ask=None):
    """Execute exactly the example the user clicked.

    `token` is the short content-addressed handle the Viewer's Run control
    carries. The identity payload it names is recovered from the private click
    registry and proved unaltered before anything else happens; the payload is
    then the same structure it has always been, and every downstream check --
    source-graph rebuild, aggregate-hash comparison, exact unit binding -- is
    unchanged.
    """
    cleanup_stale_views()

    identity = decode_click_identity(load_click_identity(token))

    exe = stata_exe()
    roots = ado_roots(exe, stata_roots)

    # Frozen section 12.1: every artifact of this click goes in one directory
    # named for the ROOT help topic, beneath the click-time c(pwd).
    parent_root = Path(parent_pwd) if parent_pwd else Path.cwd()
    out_dir = ensure_topic_directory(parent_root, str(identity.get("topic", "")))
    writable = out_dir is not None

    if out_dir is None:
        # Nothing may be written, but the caller still needs a location to
        # report, so name the directory that could not be created.
        out_dir = topic_output_directory(parent_root, str(identity.get("topic", "")))

    basename = ""
    target = None
    plan = []
    # Header lines that must appear on EVERY path once known -- the
    # INTERACTIVE line recording that the run paused for the user was written
    # on the success path only, so a paused run that ended FAILED carried no
    # evidence of its pause (HHARN-43).
    header_extra = []
    # What the code record needs and the log does not carry: the child's own
    # transcript (the only evidence of where execution reached), the units of
    # the document, and what helprun staged. Filled in as the run proceeds so
    # that every exit path -- refusal, preflight failure, failure, success --
    # writes the same record from the same evidence.
    run_state = {"transcript": "", "units": [], "interactive": None,
                 "staged_inputs": None}

    def _write_code_records(status, reason, message, log_path, artifacts):
        """The .txt code record, the optional .do, and the run manifest.

        Written after the log and the artifacts have reached their final state,
        because the manifest records their sizes and hashes and must never
        claim one for a file that was written afterwards. The manifest is last
        and is not indexed by itself.
        """
        written = {"code": "", "do": "", "manifest": ""}

        # Parse failure: helprun could not determine the authored code. No code
        # artifact is written and none is invented; the log and the classified
        # reason are the record.
        if not writable or not basename or not target or not target.get("code"):
            return written

        try:
            boundary = authored_boundary(target, plan, run_state["transcript"])

            blocker = do_representation_blocker(
                target, plan, run_state["units"],
                run_state["interactive"], run_state["staged_inputs"],
            )

            do_name = ""

            if not blocker:
                do_path = Path(out_dir) / (basename + DO_ARTIFACT_SUFFIX)
                do_path.write_text(
                    "\n".join(do_artifact_lines(target, plan)) + "\n",
                    encoding="utf-8")
                written["do"] = str(do_path)
                do_name = do_path.name

            do_note = do_artifact_note(blocker, do_name)

            sections = code_artifact_sections(
                identity, target, plan, boundary, status, reason, message, do_note)
            code_path = Path(out_dir) / (basename + CODE_ARTIFACT_SUFFIX)
            code_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
            written["code"] = str(code_path)

            indexed = [p for p in ([log_path, written["code"], written["do"]]
                                   + [str(a) for a in (artifacts or [])]) if p]
            manifest = run_manifest(identity, target, plan, boundary, status,
                                    reason, message, out_dir, indexed,
                                    do_blocker=blocker)
            manifest_path = Path(out_dir) / (basename + MANIFEST_SUFFIX)
            manifest_path.write_text(
                json.dumps(manifest, indent=1, ensure_ascii=False) + "\n",
                encoding="utf-8")
            written["manifest"] = str(manifest_path)
        except OSError:
            pass

        return written

    def refuse(error, status=STATUS_REFUSED, extra_log=()):
        """Refuse cleanly, still leaving a diagnostic log when we can."""
        log_path = ""

        if writable and basename:
            try:
                sections = _log_header(identity, target or {}, plan, extra=header_extra)
                sections.append("")
                sections.append("STATUS      : " + status)
                sections.append("FAILURE CLASS: " + error.failure_class)
                sections.append("REASON      : " + error.reason)
                sections.append("MESSAGE     : " + redact_secrets(error.message))
                if error.detail:
                    sections.append("DETAIL      : " + redact_secrets(str(error.detail)))
                sections.extend(redact_secrets(line) for line in extra_log)
                log_path = str(_write_run_log(out_dir, basename, sections))
            except OSError:
                log_path = ""

        records = _write_code_records(status, error.reason, error.message,
                                      log_path, refuse.artifacts)

        return make_outcome(
            status,
            error.reason,
            redact_secrets(error.message),
            topic=str(identity.get("topic", "")),
            ordinal=identity.get("ord", 0),
            plan=[u["ordinal"] for u in plan],
            logfile=log_path,
            output_dir=str(out_dir),
            basename=basename,
            artifacts=[str(a) for a in refuse.artifacts],
            child_output="",
            code_artifact=records["code"],
            do_artifact=records["do"],
            manifest=records["manifest"],
        )

    # Artifacts the run legitimately produced before it failed; empty on the
    # refusal paths that never executed anything.
    refuse.artifacts = []

    try:
        acquire_run_lock()
    except HelprunError as exc:
        return make_outcome(
            STATUS_REFUSED,
            exc.reason,
            exc.message,
            topic=str(identity.get("topic", "")),
            ordinal=identity.get("ord", 0),
            plan=[],
            logfile="",
            output_dir=str(out_dir),
            basename="",
            artifacts=[],
            child_output="",
        )

    try:
        if not writable:
            return make_outcome(
                STATUS_REFUSED,
                "OUTPUT_DIRECTORY_NOT_WRITABLE",
                "helprun: the current working directory is not writable, so "
                "no log or artifact could be preserved: " + str(out_dir),
                topic=str(identity.get("topic", "")),
                ordinal=identity.get("ord", 0),
                plan=[],
                logfile="",
                output_dir=str(out_dir),
                basename="",
                artifacts=[],
                child_output="",
            )

        # Source-bound identity: refuse rather than run a renumbered block.
        graph = verify_click_identity(identity, roots)

        source = Path(identity["root"])
        units = runnable_units_for(source, roots)
        run_state["units"] = units

        target = next(
            (u for u in units if u["ordinal"] == int(identity["ord"])), None
        )

        if (
            target is None
            or int(target["start"]) != int(identity["start"])
            or int(target["end"]) != int(identity["end"])
        ):
            raise HelprunError(
                "SOURCE_CHANGED",
                "helprun: the clicked example no longer matches the prepared "
                "source; reopen the help and run helprun again",
            )

        basename = choose_run_basename(
            out_dir, identity["topic"], target["ordinal"]
        )

        plan, problem = plan_prerequisites(units, target, roots)

        if problem is not None:
            plan = []
            return refuse(problem)

        # A unit that ends inside an open block cannot be run: Stata would be
        # fed an unterminated block, and an unterminated `input` would sit
        # waiting for data that never arrives until the run times out. Where the
        # block was meant to end cannot be determined without guessing, and
        # inventing the terminator would be exactly the silent rewriting
        # section 5 forbids. The unit stays visible on the page; the refusal
        # happens here, with a reason the user can act on.
        for unit in plan:
            if unit.get("open_block"):
                return refuse(
                    HelprunError(
                        "AMBIGUOUS_EXAMPLE_RECONSTRUCTION",
                        "helprun: example "
                        + str(unit["ordinal"])
                        + " ends inside an unterminated "
                        + str(unit["open_block"])
                        + " block, so where it was meant to end cannot be "
                        "determined without guessing",
                        detail=str(unit.get("heading", "")),
                    )
                )

            # A fragment the reconstruction could not read as a command, with
            # nothing open above it to justify joining. Executing it would
            # hand Stata a line that is not a command -- the r(199) `is not a
            # valid command name` failure -- and joining it would be inventing
            # the continuation the source does not supply. Section 5 permits
            # reconstructing an authored continuation and forbids inventing a
            # missing one, so neither is available: refuse, and say why.
            fragments = unit.get("unreliable_fragments") or []
            if fragments:
                return refuse(
                    HelprunError(
                        "AMBIGUOUS_EXAMPLE_RECONSTRUCTION",
                        "helprun: example "
                        + str(unit["ordinal"])
                        + " contains a line that cannot be read as a command "
                        "and that nothing above it continues, so how it was "
                        "meant to be joined cannot be determined without "
                        "guessing",
                        detail="fragment: " + redact_secrets(str(fragments[0]))[:160],
                    )
                )

        commands = []
        for unit in plan:
            commands.extend(unit["code"])

        unit_lines = [
            graph.lines[n - 1]
            for n in range(target["start"], min(target["end"], len(graph.lines)) + 1)
        ]

        ctx = {
            "source_dir": Path(source).parent,
            "roots": roots,
            "out_dir": out_dir,
            "sandbox": None,
            # The working directory the user was in when they clicked. A
            # relative authored input such as `use example_data.dta` resolves
            # against it in the parent session, and resolving it is the whole
            # point of isolating execution somewhere else: moving the run into
            # a sandbox must not make a legitimate input disappear.
            "parent_pwd": parent_root,
        }

        # Preflight: a Stata version the installation cannot provide is known
        # before anything runs, so say so rather than failing obscurely.
        wanted = required_stata_version(commands)
        running = stata_version_number()

        if wanted is not None and running is not None and wanted > running:
            return refuse(
                HelprunError(
                    "STATA_VERSION_INCOMPATIBLE",
                    "helprun: this example requires Stata "
                    + ("%g" % wanted)
                    + ", but this installation is Stata "
                    + ("%g" % running),
                )
            )

        # A declared licence requirement is a more specific explanation than
        # the guard's "component could not be resolved", so it is checked
        # first and reported instead. It is looked for across the whole help
        # document, not just the Example block, because help files declare
        # such requirements in Description or Remarks.
        licensed = help_declares_license(graph.lines)

        if licensed is not None and any(
            EXTERNAL_LAUNCH_RE.match(c) for c in commands
        ):
            return refuse(
                HelprunError(
                    "LICENSE_REQUIRED",
                    "helprun: this example needs a licensed component"
                    + (
                        " (" + licensed["component"] + ")"
                        if licensed["component"]
                        else ""
                    )
                    + "; helprun does not supply or manage licences",
                )
            )

        # Interactive input inside the authored programs (HPROD-42). Decided
        # BEFORE execution from the programs' own source. Such an example runs
        # in a visible Stata that helprun launches and owns, where the user
        # answers the example's prompt by hand; helprun itself never answers.
        needs_input = interactive_requirements(commands, roots)
        worker = None
        trace_commands = ()

        if needs_input:
            trace_commands = tuple(n["command"] for n in needs_input)
            title = worker_window_title(str(identity.get("topic", "")),
                                        target["ordinal"])

            def _tell(text):
                try:
                    from sfi import SFIToolkit
                    SFIToolkit.displayln(text, asis=True)
                except Exception:
                    pass

            # Every requirement must be a line the user can type (an _request()
            # or a pause prompt); a modal dialog cannot be relayed and takes the
            # visible route with the minimum user action (HPROD-48).
            relayable = all(n.get("primitive") in ("_request()", "pause") for n in needs_input)

            def _parent_ask(info):
                """Present the authored output and prompt in the PARENT and read
                one line there through Stata's own _request (GATE 2 R23)."""
                from sfi import SFIToolkit, Macro
                for line in (info.get("transcript") or "").splitlines():
                    if line.strip():
                        SFIToolkit.displayln(line, asis=True)
                kind = info.get("class")
                if kind == PROMPT_ENTER_ONLY:
                    _tell("helprun: the example is waiting for you -- press Enter in this "
                          "Command window to continue")
                elif kind == PROMPT_YES_NO:
                    _tell("helprun: the example is waiting for you -- type y or n in this "
                          "Command window and press Enter")
                else:
                    _tell("helprun: the example is waiting for you -- type your answer in "
                          "this Command window and press Enter")
                SFIToolkit.stata('global HELPRUN_ANSWER ""')
                try:
                    SFIToolkit.stata("display _request(HELPRUN_ANSWER)")
                except Exception:
                    raise InteractionCancelled()
                answer = Macro.getGlobal("HELPRUN_ANSWER") or ""
                try:
                    SFIToolkit.stata("macro drop HELPRUN_ANSWER")
                except Exception:
                    pass
                return answer

            # Unattended runs (validation, or a bounded wait configured) never
            # ask the parent: _request there would block for a line nobody
            # types. They receive answers from a hook, or report the prompt.
            unattended = (os.environ.get("HELPRUN_UNATTENDED", "") == "1"
                          or bool(os.environ.get("HELPRUN_INPUT_WAIT_SECONDS", "").strip()))
            if ask is None and stata_available() and not unattended:
                ask = _parent_ask

            # A requirement that is not a typed line (a modal dialog) needs the
            # worker VISIBLE from the start: a hidden Stata does not show the
            # dialog and lets the program continue as if answered (R25), which
            # would change the authored semantics. Everything else starts
            # hidden and is revealed only if a relay cannot be delivered.
            worker = {
                "title": title,
                "ask": ask,
                "relayable": relayable,
                "visible": not relayable,
                "on_start": (None if relayable else lambda pid: _tell(
                    "helprun: this example asks through a dialog that cannot be answered "
                    "here, so it is running in a separate Stata window titled \"" + title
                    + "\"; its output appears there, and here when it finishes")),
                "on_pause": lambda: _tell(
                    "helprun: the example is waiting for you -- this question cannot be "
                    "answered here; answer it in the Stata window titled \"" + title + "\""),
            }

        # Runtime and safety policy, by role and provenance.
        decisions, blocking = guard_plan(commands, ctx)

        # A command the guard marked skippable is replaced by an inert comment
        # rather than dropped, so the authored order and every later index stay
        # exactly as reconstructed. U-R13: an already-usable dependency must not
        # be reinstalled, so the install must not reach the child at all.
        if any(d.get("skip") for d in decisions):
            commands = [
                ("* helprun: skipped, " + d["evidence"]) if d.get("skip") else c
                for c, d in zip(commands, decisions)
            ]

        if blocking:
            first = blocking[0]
            return refuse(
                HelprunError(
                    first["reason"] or "UNSAFE_OPERATION_REFUSED",
                    "helprun: " + first["evidence"],
                    detail=first["command"],
                ),
                extra_log=["GUARD       : " + first["command"]],
            )

        # Credential requirement is reported, never the value.
        if any(command_requires_credential(c) for c in commands):
            return refuse(
                HelprunError(
                    "CREDENTIAL_REQUIRED",
                    "helprun: this example requires a credential that you must "
                    "supply; helprun does not store or display credential values",
                )
            )

        # Dataset resolution, with no fuzzy filename substitution.
        staged_inputs, data_problem = resolve_data_references(
            commands, ctx, unit_lines)
        run_state["staged_inputs"] = staged_inputs

        if data_problem is not None:
            return refuse(data_problem)

        # In-memory state that an authored process boundary cannot carry.
        segments = _split_process_segments(commands)
        unmet_macros = cross_process_macro_dependency(segments)

        if unmet_macros:
            return refuse(
                HelprunError(
                    "CROSS_PROCESS_STATE_DEPENDENCY",
                    "helprun: this example continues in a new Stata process but "
                    "needs in-memory values that cannot survive it: "
                    + ", ".join(unmet_macros),
                )
            )

        result = execute_units(
            exe,
            plan,
            "ex" + str(target["ordinal"]),
            source_dir=Path(source).parent,
            roots=roots,
            timeout_seconds=timeout_seconds,
            capture={"basename": basename},
            staged_inputs=staged_inputs,
            worker=worker,
            trace_commands=trace_commands,
        )

        interactive_evidence = result.get("interactive")
        run_state["interactive"] = interactive_evidence
        header_extra.extend(interactive_header_lines(interactive_evidence))
        streamed_lines = list((interactive_evidence or {}).get("streamed_lines") or [])

        child_log = ""
        if result.get("logfile"):
            try:
                child_log = Path(result["logfile"]).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                child_log = ""

        child_log = redact_secrets(child_log)

        if not result.get("pass"):
            if result.get("reason") == "EXECUTION_TIMEOUT":
                error = HelprunError(
                    "EXECUTION_TIMEOUT",
                    "helprun: this example did not finish within the time "
                    "limit and was stopped",
                )
            elif result.get("reason"):
                # The executor already classified this from real evidence;
                # do not overwrite it with a weaker guess.
                error = HelprunError(
                    result["reason"],
                    result.get("error")
                    or "helprun: the example could not be run",
                )
            else:
                no_setup = not example_provides_data_setup(
                    target or {}, getattr(graph, "lines", ())
                )

                reason, evidence = classify_child_failure(
                    child_log,
                    result.get("r_codes") or [],
                    no_data_setup=no_setup,
                )

                # AMBIGUOUS_FAILURE_PROVENANCE is the reason of last resort and
                # says only that helprun cannot tell. EXAMPLE_DATA_SETUP_MISSING
                # says something stronger and, here, false: its section 9
                # premise is that the Example supplies no dataset and no data
                # setup, and an earlier unit of this same document supplies
                # exactly that. Where the document shows the example is a
                # continuation, that precise fact is reported instead
                # (HPROD-60). The real Stata error still travels with it.
                if reason in (
                    "AMBIGUOUS_FAILURE_PROVENANCE",
                    "EXAMPLE_DATA_SETUP_MISSING",
                ) and continues_earlier_example(target, units):
                    reason = "EXAMPLE_CONTINUES_EARLIER_EXAMPLE"

                if reason == "EXAMPLE_DATA_SETUP_MISSING":
                    # Section 9 fixes the exact concise public wording. The
                    # literal Stata error, the commands and the internal
                    # classification stay in the persistent log.
                    message = (
                        "helprun: this example does not provide a runnable "
                        "dataset or data setup."
                    )
                elif reason == "EXAMPLE_CONTINUES_EARLIER_EXAMPLE":
                    message = (
                        "helprun: this example continues from an earlier "
                        "example in the same help topic, and each example runs "
                        "in its own Stata session, so what the earlier one left "
                        "in memory was not there. "
                        + (evidence or "See the log for the Stata error.")
                    )
                else:
                    message = (
                        "helprun: the example did not run to completion. "
                        + (evidence or "See the log for the Stata error.")
                    )

                error = HelprunError(reason, message)

            # Artifacts the authored commands created before the failure are
            # final outputs too (specification 12.3, HPROD-49): preserved and
            # verified BEFORE the log is written and the sandbox removed.
            artifacts, not_exported = _export_artifacts(result, out_dir, basename)
            # The code record is written from the same evidence on this path as
            # on every other: the child's transcript decides the boundary, and
            # the artifacts the run legitimately produced are indexed with it.
            # The boundary is judged from the SAME transcript the user's log
            # shows. Judging from the raw child log instead let the record say
            # a command did not execute while the log the user opens shows it
            # running -- a contradiction the user would have to resolve, and
            # the one thing an execution record must never produce.
            run_state["transcript"] = user_facing_transcript(child_log)
            refuse.artifacts = artifacts
            outcome = refuse(
                error,
                status=STATUS_FAILED,
                extra_log=["", "OUTPUT", "-" * 60,
                           user_facing_transcript(child_log), "",
                           "ARTIFACTS   : " + (", ".join(Path(a).name for a in artifacts) or "none")]
                          + (["ARTIFACTS NOT EXPORTED: " + ", ".join(not_exported)
                              + " -- sandbox kept: " + str(result.get("sandbox", ""))]
                             if not_exported else []),
            )
            outcome["artifacts"] = [str(a) for a in artifacts]
            # the orchestration record travels on the outcome, not on the log
            outcome["interactive_internal"] = interactive_internal_record(interactive_evidence)
            outcome["child_output"] = child_log
            outcome["sandbox"] = result.get("sandbox", "")
            outcome["temp_root"] = result.get("temp_root", "")
            outcome["child_temp"] = result.get("child_temp", "")
            outcome["r_codes"] = result.get("r_codes", [])
            outcome["segments"] = result.get("segments", 0)
            outcome["child_pids"] = result.get("child_pids", [])
            outcome["interactive"] = interactive_evidence
            outcome["streamed_lines"] = streamed_lines

            # The diagnostic log and the artifacts are written, so the sandbox
            # can go -- unless an identified artifact could not be preserved
            # (HPROD-50: cleanup never destroys the only required copy).
            if not not_exported:
                _cleanup_sandbox(result.get("sandbox"))
            return outcome

        # Success: export verified artifacts to the frozen output directory,
        # then remove the sandbox -- kept if an artifact could not be preserved.
        artifacts, not_exported = _export_artifacts(result, out_dir, basename)
        if not not_exported:
            _cleanup_sandbox(result.get("sandbox"))

        sections = _log_header(identity, target, plan)
        # Evidence that the example paused for the user, what it asked, what
        # was relayed and that it resumed: kept in the header because the
        # trace that showed it is stripped from every user-facing surface.
        sections.extend(interactive_header_lines(interactive_evidence))
        sections.append("")
        sections.append("STATUS      : " + STATUS_SUCCESS)
        sections.append("COMMANDS EXECUTED")
        sections.append("-" * 60)
        sections.extend(redact_secrets(c) for c in commands)
        sections.append("")
        # The user-facing log carries the clean transcript. The raw child
        # output is returned on the outcome as `child_output` for internal,
        # debug and validation use; it is not what a user opens.
        sections.append("OUTPUT")
        sections.append("-" * 60)
        sections.append(user_facing_transcript(child_log))
        sections.append("")
        sections.append(
            "ARTIFACTS   : "
            + (", ".join(Path(a).name for a in artifacts) or "none")
        )
        if not_exported:
            sections.append("ARTIFACTS NOT EXPORTED: " + ", ".join(not_exported)
                            + " -- sandbox kept: " + str(result.get("sandbox", "")))

        # HPROD-68. The refusal path a few hundred lines above writes its log
        # under `if writable and basename:` inside a try/except OSError, and
        # degrades to an empty log_path when the destination cannot be
        # written. This success path did neither, so a destination that was
        # unwritable -- or that vanished between ensure_topic_directory() and
        # here, which is what happened to a TEMP working root mid-run -- raised
        # FileNotFoundError straight out of click_run. run_public() converts an
        # escaped exception into a clean message, so the user saw no traceback,
        # but the run was reported as an internal error rather than as the
        # successful run it actually was, and a caller invoking click_run
        # directly got the exception. The two paths now degrade identically:
        # losing the log must not lose the run.
        # The condition is named rather than written inline: the refusal path's
        # `if writable and basename:` is the anchor mutant M18 targets, and a
        # second identical line here made that anchor ambiguous, so M18 could
        # no longer be applied. Same behaviour, distinct text.
        may_write_log = bool(writable) and bool(basename)
        log_path = ""
        if may_write_log:
            try:
                log_path = _write_run_log(out_dir, basename, sections)
            except OSError:
                log_path = ""

        # Same transcript the log shows, for the same reason as on the failed
        # path: the record and the log must never contradict each other.
        run_state["transcript"] = user_facing_transcript(child_log)
        records = _write_code_records(STATUS_SUCCESS, "", "",
                                      str(log_path), artifacts)

        return make_outcome(
            STATUS_SUCCESS,
            "",
            "",
            code_artifact=records["code"],
            do_artifact=records["do"],
            manifest=records["manifest"],
            topic=str(identity.get("topic", "")),
            ordinal=target["ordinal"],
            plan=[u["ordinal"] for u in plan],
            logfile=str(log_path),
            output_dir=str(out_dir),
            basename=basename,
            artifacts=[str(a) for a in artifacts],
            child_output=child_log,
            sandbox=result.get("sandbox", ""),
            temp_root=result.get("temp_root", ""),
            child_temp=result.get("child_temp", ""),
            r_codes=result.get("r_codes", []),
            segments=result.get("segments", 0),
            interactive=interactive_evidence,
            interactive_internal=interactive_internal_record(interactive_evidence),
            streamed_lines=streamed_lines,
        )

    except HelprunError as exc:
        return refuse(exc)

    finally:
        release_run_lock()


def _cleanup_sandbox(sandbox):
    """Remove one run's sandbox once its evidence has been exported.

    Specification section 15: sandboxes must not accumulate. Cleanup happens
    only after the user-visible log and artifacts are already written, so no
    failure evidence is destroyed before it has been captured.
    """
    if not sandbox:
        return False

    try:
        shutil.rmtree(str(sandbox), ignore_errors=True)
    except OSError:
        return False

    return True


def _copy_verified(path, destination):
    """Copy one artifact and verify the copy's size against the source; a copy
    that does not verify is removed. Returns True only for a verified copy."""
    try:
        shutil.copyfile(path, destination)
        if destination.stat().st_size != path.stat().st_size:
            try:
                destination.unlink()
            except OSError:
                pass
            return False
        return True
    except OSError:
        return False


def _export_artifacts(result, out_dir, basename):
    """Copy verified final artifacts out of the sandbox, never the whole tree.

    Called on EVERY path (SUCCESS and FAILED) before the sandbox is removed
    (HPROD-49/50). Returns (exported, failed): `failed` names artifacts that
    were identified but could not be preserved -- the caller then keeps the
    sandbox rather than destroy the only copy.
    """
    exported = []
    failed = []

    sandbox = result.get("sandbox")
    if not sandbox:
        return exported, failed

    sandbox = Path(sandbox)
    capture_dir = sandbox / "_hr_out"

    if capture_dir.is_dir():
        for path in sorted(capture_dir.iterdir()):
            if not path.is_file():
                continue
            destination = Path(out_dir) / path.name
            if destination.exists():
                continue
            if _copy_verified(path, destination):
                exported.append(destination)
            else:
                failed.append(path.name)

    # Captured Stata graph objects (specification 12.2). The child saved every
    # graph it saw at every snapshot, so the same unchanged graph appears many
    # times; distinct CONTENT in creation order is what the user authored, and
    # a graph replaced under one name is therefore two artifacts rather than
    # one. This runs before the sandbox is removed and is independent of the
    # authored-file collector below: an authored .png or .pdf the example
    # exported itself never satisfies, replaces or suppresses a .gph
    # (HPROD-54).
    graph_dir = sandbox / GRAPH_SNAPSHOT_DIR
    if graph_dir.is_dir():
        render_missing_graph_images(graph_dir)
        seen_content = set()
        index = 0
        for gph in sorted(graph_dir.glob("*.gph")):
            try:
                content = hashlib.sha256(gph.read_bytes()).hexdigest()
            except OSError:
                failed.append(gph.name)
                continue
            if content in seen_content:
                continue
            seen_content.add(content)
            index += 1
            stem = "%s-graph-%d" % (basename, index)
            gdest = Path(out_dir) / (stem + ".gph")
            if _copy_verified(gph, gdest):
                exported.append(gdest)
            else:
                failed.append(gph.name)
                continue
            png = gph.with_suffix(".png")
            if png.is_file():
                pdest = Path(out_dir) / (stem + ".png")
                if _copy_verified(png, pdest):
                    exported.append(pdest)
                else:
                    failed.append(png.name)

    ordinal = 0
    for path, normalize in collect_authored_artifacts(
        sandbox, result.get("pre_existing", set())
    ):
        if normalize:
            # a runtime-named temporary is preserved under the run's identity,
            # numbered in creation order (specification 12.3)
            ordinal += 1
            destination = Path(out_dir) / ("%s-artifact-%d%s" % (basename, ordinal, path.suffix.lower()))
            if destination.exists():
                failed.append(path.name)
                continue
        else:
            destination = Path(out_dir) / path.name
            if destination.exists():
                destination = Path(out_dir) / (basename + "-" + path.name)
            if destination.exists():
                continue

        if _copy_verified(path, destination):
            exported.append(destination)
        else:
            failed.append(path.name)

    return exported, failed


def _flatten_for_ado(result):
    """Flatten an outcome to plain strings for Stata local macros."""
    flat = {}

    for key, value in result.items():
        if isinstance(value, bool):
            flat[key] = "1" if value else "0"
        elif isinstance(value, (list, tuple)):
            flat[key] = " ".join(str(x) for x in value)
        elif value is None:
            flat[key] = ""
        else:
            flat[key] = str(value)

    return flat


def ado_prepare(roots):
    """Single entry point for helprun.ado's preparation call.

    helprun.ado must invoke Python with one-line `python:` statements, because
    a `python:` ... `end` block placed inside `program define ... end` would
    have its `end` close the program definition instead of the Python block,
    and the ado file then fails to load.
    """
    return _flatten_for_ado(prepare_public(roots))


def ado_click(token, parent_pwd, roots):
    """Single entry point for helprun.ado's click call.

    Section 12.1A shapes what the parent Results window may contain. This is
    where the outcome is turned into exactly two things the ado can show:

      resultsfile  the authored commands and their ordinary Stata output, once,
                   with the batch banner, the run-log header and helprun's own
                   graph instrumentation removed
      location     the single concise absolute directory line

    Everything else -- the literal child transcript, provenance, hashes,
    internal classification and the artifact manifest -- stays in the
    persistent log and never reaches Results.
    """
    outcome = run_public(token, parent_pwd, roots)

    transcript = results_transcript(outcome.get("child_output", ""))
    # What the parent already showed while relaying prompts is not shown again
    # (HPROD-48): the final transcript starts after the streamed prefix.
    transcript = strip_streamed_prefix(transcript, outcome.get("streamed_lines") or [])

    # On failure, prefer the meaningful causal Stata message over the
    # `end of do-file` noise the batch log ends with.
    causal, code = causal_stata_error(outcome.get("child_output", ""))
    outcome["causal"] = causal
    outcome["rcode"] = code

    results_path = ""
    if transcript:
        try:
            handle, tmp = tempfile.mkstemp(prefix="helprun_results_", suffix=".txt")
            os.close(handle)
            Path(tmp).write_text(transcript + "\n", encoding="utf-8")
            results_path = tmp
        except OSError:
            results_path = ""

    outcome["resultsfile"] = results_path

    # One concise location line, using the actual absolute topic directory.
    out_dir = str(outcome.get("output_dir", "") or "")
    location = ""
    if out_dir:
        shown = out_dir if out_dir.endswith(("\\", "/")) else out_dir + "\\"
        if outcome.get("status") == STATUS_SUCCESS:
            # The destination holds the run log plus any graphs and
            # authored artifacts, so the line names what is actually there.
            location = "helprun: log and other output files saved in " + shown
        elif outcome.get("logfile"):
            location = "helprun: diagnostic log saved in " + shown
    outcome["location"] = location

    # The raw child transcript must not travel through a Stata macro.
    outcome.pop("child_output", None)
    # structured interactive evidence is for the log header and validation,
    # not for Stata locals
    outcome.pop("interactive", None)
    outcome.pop("interactive_internal", None)
    outcome.pop("streamed_lines", None)

    return _flatten_for_ado(outcome)


def run_public(token, parent_pwd=None, stata_roots=None):
    """Stata-facing wrapper. No Python traceback may escape into the UI."""
    try:
        outcome = click_run(token, parent_pwd, stata_roots)
        outcome["ok"] = outcome.get("status") == STATUS_SUCCESS
        outcome.setdefault("error", outcome.get("message", ""))
        return outcome

    except Exception as exc:
        return make_outcome(
            STATUS_FAILED,
            "HELPRUN_INTERNAL_ERROR",
            "helprun: " + str(exc),
            ok=False,
            topic="",
            ordinal=0,
            plan=[],
            logfile="",
            output_dir="",
            basename="",
            artifacts=[],
            child_output="",
            error="helprun: " + str(exc),
        )
