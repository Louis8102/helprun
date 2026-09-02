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
    "HELPRUN_BUSY": CLASS_EXECUTION,
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

        collides = any(
            name == prefix or name.startswith(prefix + ".")
            or name.startswith(prefix + "-graph-")
            for name in existing
        )

        if not collides:
            return candidate

    raise HelprunError(
        "OUTPUT_ARTIFACT_MISSING",
        "helprun: could not find a collision-free output basename",
    )


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
    at brace depth 0 and outside quotes.  display_text is None for syntax 3."""
    depth = 0
    in_quote = False

    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == '"' and depth == 0:
            in_quote = not in_quote
        elif ch == ":" and depth == 0 and not in_quote:
            return body[:i], body[i + 1:]

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


def line_is_marked_command(raw, at_paragraph_start=True):
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
    index = 0

    while index < length:
        if stripped[index] != "{":
            index += 1
            continue

        name = span_name_at(index)

        if name not in _INLINE_SPAN_NAMES:
            return False

        close = _matching_brace(stripped, index)

        if close < 0:
            return False

        index = close + 1

    return True


def native_link_commands(raw):
    """Commands contributed by native {stata ...} / {matacmd ...} links.

    A native link is a runnable command by the documented SMCL definition
    (GATE 2 R02): the author already declared it executable. Its indentation
    therefore carries no information, and the indentation heuristics used to
    recognise plain-text code must not be applied to it.
    """
    if hidden(raw):
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
    inside = False

    for line_no, raw in enumerate(
        lines,
        start=1
    ):
        if examples_container(raw):
            inside = True
            containers.append(
                (line_no, extract_title(raw))
            )
            continue

        if titled_example(raw):
            inside = True
            boundaries.append(
                (line_no, extract_title(raw))
            )
            continue

        if not inside:
            continue

        heading = visible_example_heading(raw)
        if heading is not None:
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


class CommandList(list):
    """Reconstructed commands, plus whether they end inside an open block."""

    __slots__ = ("open_block",)

    def __init__(self, items=()):
        super().__init__(items)
        self.open_block = None


def reconstruct_unit(path, unit, roots):
    lines = read_help_lines(path, roots)
    commands = []
    current = None
    block_mode = None
    comment_join_pending = False
    alternative_pending = False
    para_start = True

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

        if hidden(raw):
            continue

        visible = render(raw)

        if not visible:
            continue

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
            or line_is_marked_command(raw, here_para_start)
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
                continue

            else:
                flush_current()

        if plausible_indented(raw, visible) or line_is_marked_command(
            raw, here_para_start
        ):
            if (
                visible_example_heading(raw)
                is not None
            ):
                continue

            current = stripped

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


def execute_units(
    exe,
    selected_units,
    label,
    source_dir=None,
    roots=None,
    timeout_seconds=90,
    capture=None
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

        plan.write_text(
            "\n".join(build_child_plan(segment, segment_capture)) + "\n",
            encoding="utf-8",
        )

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
        timeout = False

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
            and p.returncode == 0
            and segment_log_file.exists()
            and not segment_r_codes
        )

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
                "child_pid": child_pids[-1],
                "child_pids": child_pids,
                "sandbox": str(sandbox),
                "temp_root": str(child_temp),
                "logfile": str(combined_log) if combined_log.exists() else "",
                "r_codes": all_r_codes,
                "pre_existing": pre_existing,
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
        "child_pid": child_pids[-1],
        "child_pids": child_pids,
        "sandbox": str(sandbox),
        "temp_root": str(child_temp),
        "logfile": str(sandbox / "plan.log"),
        "r_codes": all_r_codes,
        "pre_existing": pre_existing,
        "child_temp": str(child_temp),
        "segments": len(segments),
        "error": "",
    }


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

    needs_data = bool(unit_varlist_candidates(target)) or any(
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
            for token in unit_varlist_candidates(target)
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
    }

    if not s or s.startswith("*") or s.startswith("//"):
        return decision

    if DATA_LOAD_RE.match(s):
        decision["dependency_class"] = DEP_DATA_SETUP

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

NETWORK_FAILURE_PHRASES = (
    "host not found",
    "could not connect",
    "unable to connect",
    "connection timed out",
    "no such host",
    "server refused",
    "web resource not found",
)


def classify_child_failure(log_text, r_codes, known_missing_vars=None):
    """Map a child failure to (reason, evidence line) from real evidence."""
    text = log_text or ""
    low = text.lower()

    error_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith(". ")
    ]

    evidence = ""
    for line in reversed(error_lines):
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
        return "STATA_VERSION_INCOMPATIBLE", evidence

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
            r'^copy\s+(?:"[^"]+"|\S+)\s+("[^"]+"|[^\s,]+)',
            s,
            flags=re.IGNORECASE,
        )
        if m:
            created.add(_unquote_path(m.group(1)).lower())

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

        for base in (
            ctx.get("sandbox"),
            ctx.get("source_dir"),
            ctx.get("out_dir"),
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


def graph_capture_preamble(order_macro="HR_GORDER"):
    return [
        "capture program drop _hr_gsnap",
        "program define _hr_gsnap",
        "    capture quietly graph dir",
        "    if _rc {",
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
        "end",
        "global " + order_macro + ' ""',
    ]


def graph_capture_postamble(out_dir, basename, order_macro="HR_GORDER"):
    target = str(out_dir).replace("\\", "/")

    # GATE 2 established the reliable sequence: save first, then load the
    # saved graph and export it. A bare `graph export ..., name()` fails with
    # r(693) "could not find Graph window" for a nodraw graph in batch mode.
    stem = target + "/" + basename + "-graph-"

    return [
        "capture _hr_gsnap",
        "local _hri = 0",
        "foreach g of global " + order_macro + " {",
        "    local ++_hri",
        '    capture noisily graph save `g\' "' + stem + '`_hri\'.gph", replace',
        '    capture noisily graph use "' + stem + '`_hri\'.gph"',
        '    capture noisily graph export "' + stem + '`_hri\'.png", replace width(1200)',
        "}",
    ]


def build_child_plan(commands, capture):
    """Interleave read-only capture instrumentation into one child plan."""
    if not capture:
        return list(commands)

    out = list(graph_capture_preamble())
    flags = top_level_flags(commands)

    for command, safe in zip(commands, flags):
        out.append(command)
        if safe:
            out.append("capture _hr_gsnap")

    out.extend(
        graph_capture_postamble(capture["out_dir"], capture["basename"])
    )

    return out


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
}


def collect_authored_artifacts(sandbox, pre_existing):
    """Files the example itself created that look like final artifacts.

    Temporary caches, runtime files and helprun's own plan/log files are never
    exported (specification section 12.3).
    """
    found = []
    sandbox = Path(sandbox)

    for path in sorted(sandbox.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(sandbox)

        if rel.parts and rel.parts[0] == "_tmp":
            continue
        if rel.parts and rel.parts[0] == "_hr_out":
            continue
        if path.name in HELPRUN_INTERNAL_NAMES:
            continue
        if path.suffix.lower() not in AUTHORED_ARTIFACT_EXTENSIONS:
            continue
        if str(rel).lower() in pre_existing:
            continue

        found.append(path)

    return found


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

HELPRUN_VERSION = "2.0.0"


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
        try:
            code = reconstruct_unit(source, raw_unit, roots)
        except HelprunError as exc:
            skipped.append(
                {"heading": raw_unit.get("heading", ""), "reason": exc.reason}
            )
            continue

        if not code:
            continue

        unit = dict(raw_unit)
        unit["code"] = list(code)
        # Carried so click_run can refuse a unit that ends inside an open
        # block, without that refusal costing the unit its place on the page.
        unit["open_block"] = getattr(code, "open_block", None)
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


def _run_link_line(token):
    return (
        "{p 8 8 2}({stata helprun, hrclick("
        + token
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

    for unit in units:
        identity = build_click_identity(topic, doc["source"], graph, unit)
        token = encode_click_identity(identity)
        insertion_after.setdefault(unit["end"], []).append(token)

    out = []
    link_count = 0

    for line_no, raw in enumerate(lines, start=1):
        # The author's visible content is preserved verbatim, in order, with
        # native {stata ...} links untouched.
        out.append(str(raw))

        for token in insertion_after.get(line_no, []):
            out.extend(["", _run_link_line(token), ""])
            link_count += 1

    if link_count != len(units):
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
    ]
    lines.extend(extra)
    lines.append("=" * 60)
    return lines


def click_run(token, parent_pwd=None, stata_roots=None, timeout_seconds=90):
    """Execute exactly the example the user clicked."""
    cleanup_stale_views()

    identity = decode_click_identity(token)

    exe = stata_exe()
    roots = ado_roots(exe, stata_roots)

    out_dir = Path(parent_pwd) if parent_pwd else Path.cwd()
    writable = output_directory_writable(out_dir)

    basename = ""
    target = None
    plan = []

    def refuse(error, status=STATUS_REFUSED, extra_log=()):
        """Refuse cleanly, still leaving a diagnostic log when we can."""
        log_path = ""

        if writable and basename:
            try:
                sections = _log_header(identity, target or {}, plan)
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
            artifacts=[],
            child_output="",
        )

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

        # Runtime and safety policy, by role and provenance.
        _decisions, blocking = guard_plan(commands, ctx)

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
        _staged, data_problem = resolve_data_references(commands, ctx, unit_lines)

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
        )

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
                reason, evidence = classify_child_failure(
                    child_log, result.get("r_codes") or []
                )
                error = HelprunError(
                    reason,
                    "helprun: the example did not run to completion. "
                    + (evidence or "See the log for the Stata error."),
                )

            outcome = refuse(
                error,
                status=STATUS_FAILED,
                extra_log=["", "CHILD OUTPUT", "-" * 60, child_log],
            )
            outcome["child_output"] = child_log
            outcome["sandbox"] = result.get("sandbox", "")
            outcome["temp_root"] = result.get("temp_root", "")
            outcome["child_temp"] = result.get("child_temp", "")
            outcome["r_codes"] = result.get("r_codes", [])
            outcome["segments"] = result.get("segments", 0)
            outcome["child_pids"] = result.get("child_pids", [])

            # The diagnostic log is already written, so the sandbox can go.
            _cleanup_sandbox(result.get("sandbox"))
            return outcome

        # Success: export verified artifacts to the frozen output directory.
        artifacts = _export_artifacts(result, out_dir, basename)
        _cleanup_sandbox(result.get("sandbox"))

        sections = _log_header(identity, target, plan)
        sections.append("")
        sections.append("STATUS      : " + STATUS_SUCCESS)
        sections.append("COMMANDS EXECUTED")
        sections.append("-" * 60)
        sections.extend(redact_secrets(c) for c in commands)
        sections.append("")
        sections.append("CHILD OUTPUT")
        sections.append("-" * 60)
        sections.append(child_log)
        sections.append("")
        sections.append(
            "ARTIFACTS   : "
            + (", ".join(Path(a).name for a in artifacts) or "none")
        )

        log_path = _write_run_log(out_dir, basename, sections)

        return make_outcome(
            STATUS_SUCCESS,
            "",
            "",
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


def _export_artifacts(result, out_dir, basename):
    """Copy verified final artifacts out of the sandbox, never the whole tree."""
    exported = []

    sandbox = result.get("sandbox")
    if not sandbox:
        return exported

    sandbox = Path(sandbox)
    capture_dir = sandbox / "_hr_out"

    if capture_dir.is_dir():
        for path in sorted(capture_dir.iterdir()):
            if not path.is_file():
                continue
            destination = Path(out_dir) / path.name
            if destination.exists():
                continue
            try:
                shutil.copyfile(path, destination)
                exported.append(destination)
            except OSError:
                pass

    for path in collect_authored_artifacts(
        sandbox, result.get("pre_existing", set())
    ):
        destination = Path(out_dir) / path.name

        if destination.exists():
            destination = Path(out_dir) / (basename + "-" + path.name)

        if destination.exists():
            continue

        try:
            shutil.copyfile(path, destination)
            exported.append(destination)
        except OSError:
            pass

    return exported


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
    """Single entry point for helprun.ado's click call."""
    return _flatten_for_ado(run_public(token, parent_pwd, roots))


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
