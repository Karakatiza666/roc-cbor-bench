"""roc-cbor-bench: a diagnostic for roc-cbor, with CBOR libraries of Rust, Go, Python and Zig as reference points.

It is a tool to find slow paths and compiler issues, not a claim: its results are not committed.

Usage: ROC=/path/to/roc python3 run.py [--refresh] [--only IMPL ...] [--cases CASE ...]
       [--runs N] [--ms MS] [--timeout S] [--no-build]

Each implementation is a program `<impl> <case> <loops>` that prints "case loops checksum". The input of
each case is a fixture written by the Roc bench of roc-cbor (roc-cbor/bench/bench.roc `dump`), so each
side reads the same bytes. The runner times each program from the outside, the same way for each language
(roc-cbor's scripts/bench.sh does the same for Roc alone):

    ms per op   = (median wall time of runs with N loops - median of runs with 0 loops) / N
    cpu per op  = the same with user + sys CPU time (more than ms per op when a GC runs on other threads)
    MB/s        = fixture bytes / ms per op (decimal MB: 1e6 bytes)
    base RSS    = peak RSS of a run with 0 loops (runtime + the fixture + the setup of the case)
    op RSS      = peak RSS of a run with 1 loop - base RSS (the memory of one op at its peak)
    peak RSS N  = peak RSS of the run with N loops (growth over op RSS: a GC heap or a leak)

Before a case is timed, `<impl> <case> check` prints the CBOR (hex) of the result of one op: the parsed value
encoded again, or the output of an encode. The runner decodes it and compares it with the fixture (the
`unknown_parse` record keeps its 5 fields). Then each timed run must print loops * the checksum of one op
(the item count of a parse, 1 for a dynamic value, the output length of an encode). A failed parse adds
FAIL = 2^40, so it cannot pass. A case that fails a check is "wrong" in the report and is not timed.

The results of the other languages are cached in .cache/results/<impl>.json (not committed), since
they do not change with roc-cbor. A cached result is used when its key matches: the hash of the fixtures, of the
source and lock files of the implementation, of its toolchain version, and the CPU model. --refresh measures
them again. Roc is measured on each run; its result goes to .cache/roc.json. The report (Markdown) is printed
and written to .cache/report.md. An implementation whose toolchain is missing or whose build fails is skipped
with a message.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import statistics
import struct
import subprocess
import sys
import time

# This repository; roc-cbor is its git submodule roc-cbor/ (the commit that is measured).
ROOT = os.path.dirname(os.path.abspath(__file__))
HERE = ROOT
ROC_CBOR = os.path.join(ROOT, "roc-cbor")
WORK = os.path.join(ROOT, ".cache")
FIX = os.path.join(WORK, "fixtures")
RESULTS = os.path.join(WORK, "results")
# The Roc compiler: the nightly of roc-cbor/.roc-version (checked in main).
ROC = os.environ.get("ROC", "roc")

# A failed parse adds this to the checksum, in each implementation: no expected checksum can be a multiple of it.
FAIL = 1 << 40
# Limits of one subprocess, in seconds: a build, and a run of a benchmark (--timeout).
BUILD_TIMEOUT = 1800
RUN_TIMEOUT = 120

# case: (fixture, kind, checksum of one op of a typed parse: its item count)
CASES = {
    "bytes_encode": ("bytes", "encode", None),
    "bytes_parse": ("bytes", "typed", 1_000_000),
    "records_encode": ("records", "encode", None),
    "records_parse": ("records", "typed", 10_000),
    "records_value": ("records", "value", None),
    "unknown_parse": ("wide", "typed", 10_000),
    "dict_parse": ("dict", "typed", 100_000),
    "map_value": ("dict", "value", None),
    "text_parse": ("texts", "typed", 10_000),
    "deep_encode": ("deep", "encode", None),
    "deep_parse": ("deep", "value", None),
}
FIXTURES = sorted({f for f, _, _ in CASES.values()})
# The fields of Rec: what `unknown_parse` keeps of each record of `wide`.
REC_FIELDS = ("id", "name", "ok", "score", "tags")

CASE_DOC = {
    "bytes_encode": "a 1 MB byte string",
    "bytes_parse": "a 1 MB byte string",
    "records_encode": "10k records {id, name, ok, score, tags}",
    "records_parse": "10k records into a typed list",
    "records_value": "10k records into a dynamic value",
    "unknown_parse": "10k records with 12 unknown fields each (skipped)",
    "dict_parse": "a map of 100k text keys into a hash map",
    "map_value": "a map of 100k text keys into a dynamic value",
    "text_parse": "10k multi-byte UTF-8 texts",
    "deep_encode": "500 nested arrays (dynamic value)",
    "deep_parse": "500 nested arrays (dynamic value)",
}

# The environment of each subprocess: temporary files under .cache/tmp, not /tmp.
TMP = os.path.join(WORK, "tmp")
BASE_ENV = {"TMPDIR": TMP, "TMP": TMP, "TEMP": TMP}


class BuildError(Exception):
    pass


def env_of(extra=None):
    e = dict(os.environ)
    e.update(BASE_ENV)
    e.update(extra or {})
    return e


def sh(cmd, cwd=ROOT, env=None, timeout=BUILD_TIMEOUT):
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env_of(env), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BuildError(f"timed out after {timeout} s: {' '.join(cmd)}")
    except OSError as e:
        raise BuildError(f"{e}: {' '.join(cmd)}")
    if r.returncode != 0:
        raise BuildError(f"failed: {' '.join(cmd)}\n{(r.stdout[-3000:] + r.stderr[-3000:]).strip()}")
    return r.stdout


def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def first_line(cmd, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env_of(env),
                              timeout=60).stdout.strip().splitlines()[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return "?"


# The implementations ---------------------------------------------------------------------------

ROC_ENV = {
    "ROC_CACHE_DIR": os.environ.get("ROC_CACHE_DIR", os.path.join(WORK, "roc")),
    # roc build stops with PathOutsideWorkspace when this path has `..` in it.
    "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", os.path.join(WORK, "xdg")),
}
PYLIB = os.path.join(WORK, "pylib")
PY_REQ = os.path.join(HERE, "python", "requirements.txt")
ZIG_ENV = {"ZIG_GLOBAL_CACHE_DIR": os.path.join(WORK, "zig-global")}
# GOTOOLCHAIN=local: use the installed go; go.mod asks for 1.26.5, and an older go fails (no download).
GO_ENV = {"GOTOOLCHAIN": "local", "GOMODCACHE": os.path.join(WORK, "go-mod"), "GOCACHE": os.path.join(WORK, "go-build"),
          "GOFLAGS": "-mod=readonly"}


def lock_version(path, name, pattern):
    try:
        with open(path) as f:
            m = re.search(pattern.format(re.escape(name)), f.read())
        return m.group(1) if m else "?"
    except OSError:
        return "?"


class Impl:
    def __init__(self, name, lang, lib, sources, build, argv, version, toolchain, tools, env=None):
        self.name, self.lang, self.lib = name, lang, lib
        self.sources, self.build, self.argv = sources, build, argv
        self.version, self.toolchain, self.tools, self.env = version, toolchain, tools, env or {}

    def missing(self):
        """Why this implementation cannot run: the tools that are not on PATH (a tool can also be a function
        that gives a reason or None)."""
        out = []
        for t in self.tools:
            if callable(t):
                why = t()
                if why:
                    out.append(why)
            elif shutil.which(t) is None:
                out.append(f"no {t} on PATH")
        return out

    def key(self, fixtures_sha, cpu, toolchain):
        h = hashlib.sha256()
        for s in self.sources:
            with open(os.path.join(HERE, s), "rb") as f:
                h.update(s.encode() + b"\0" + f.read())
        h.update(fixtures_sha.encode() + b"\0" + cpu.encode() + b"\0" + toolchain.encode())
        return h.hexdigest()[:16]


def build_rust():
    sh(["cargo", "build", "--release", "--locked", "--quiet"], cwd=os.path.join(HERE, "rust"),
       env={"CARGO_TARGET_DIR": os.path.join(WORK, "rust-target")})


def build_go():
    sh(["go", "build", "-o", os.path.join(WORK, "go-bench"), "."], cwd=os.path.join(HERE, "go"), env=GO_ENV)


def build_python():
    """cbor2 from its pinned wheels into .cache/pylib, again when requirements.txt changes."""
    with open(PY_REQ, "rb") as f:
        want = hashlib.sha256(f.read()).hexdigest()
    stamp = os.path.join(PYLIB, ".requirements.sha256")
    try:
        with open(stamp) as f:
            if f.read().strip() == want:
                return
    except OSError:
        pass
    shutil.rmtree(PYLIB, ignore_errors=True)
    flags = ["--require-hashes", "--only-binary", ":all:", "--no-deps", "--target", PYLIB, "-r", PY_REQ]
    if has_pip():
        sh([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-cache-dir"] + flags)
    else:
        sh(["uv", "pip", "install", "--quiet", "--no-cache", "--python", sys.executable] + flags)
    with open(stamp, "w") as f:
        f.write(want + "\n")


def has_pip():
    return subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, env=env_of(),
                          timeout=60).returncode == 0


def python_installer():
    """None when cbor2 can be installed: pip of this Python, else uv."""
    if has_pip() or shutil.which("uv"):
        return None
    return f"no pip module for {sys.executable} and no uv on PATH"


def python_version():
    """The version of the installed cbor2 (its dist-info in .cache/pylib)."""
    try:
        for d in os.listdir(PYLIB):
            m = re.fullmatch(r"cbor2-(.+)\.dist-info", d)
            if m:
                return m.group(1)
    except OSError:
        pass
    return lock_version(PY_REQ, "cbor2", r"{}==([^\s\\]+)")


def build_zig():
    sh(["zig", "build", "--prefix", os.path.join(WORK, "zig-out"), "--cache-dir", os.path.join(WORK, "zig-cache")],
       cwd=os.path.join(HERE, "zig"), env=ZIG_ENV)


def build_roc_dump():
    """roc-cbor/bench/bench.roc: its `dump` mode writes the fixtures."""
    sh([ROC, "build", "--opt=speed", "roc-cbor/bench/bench.roc", f"--output={os.path.join(WORK, 'roc-dump.bin')}"],
       env=ROC_ENV)


def build_roc():
    """roc/compare.roc: the cases, with the fixtures embedded (build after write_fixtures)."""
    sh([ROC, "build", "--opt=speed", "roc/compare.roc", f"--output={os.path.join(WORK, 'roc-compare.bin')}"],
       env=ROC_ENV)


RUST_BIN = os.path.join(WORK, "rust-target", "release", "cbor-bench")
CARGO_LOCK = os.path.join(HERE, "rust", "Cargo.lock")
CARGO_V = r'name = "{}"\nversion = "([^"]+)"'

IMPLS = [
    Impl("roc", "Roc", "roc-cbor", [], build_roc,
         lambda c, n: [os.path.join(WORK, "roc-compare.bin"), c, str(n)],
         lambda: first_line(["git", "-C", ROC_CBOR, "describe", "--tags", "--always", "--dirty"]),
         lambda: first_line([ROC, "version"]),
         [lambda: None if shutil.which(ROC) else f"no Roc compiler `{ROC}` (set ROC=/path/to/roc)"]),
    Impl("rust-ciborium", "Rust", "ciborium", ["rust/src/main.rs", "rust/Cargo.lock"], build_rust,
         lambda c, n: [RUST_BIN, "ciborium", c, str(n), FIX],
         lambda: lock_version(CARGO_LOCK, "ciborium", CARGO_V), lambda: first_line(["rustc", "--version"]),
         ["cargo", "rustc"]),
    Impl("rust-cbor4ii", "Rust", "cbor4ii", ["rust/src/main.rs", "rust/Cargo.lock"], build_rust,
         lambda c, n: [RUST_BIN, "cbor4ii", c, str(n), FIX],
         lambda: lock_version(CARGO_LOCK, "cbor4ii", CARGO_V), lambda: first_line(["rustc", "--version"]),
         ["cargo", "rustc"]),
    Impl("go-fxamacker", "Go", "fxamacker/cbor", ["go/main.go", "go/go.mod", "go/go.sum"], build_go,
         lambda c, n: [os.path.join(WORK, "go-bench"), c, str(n), FIX],
         lambda: lock_version(os.path.join(HERE, "go", "go.mod"), "github.com/fxamacker/cbor/v2", r"{} (\S+)"),
         lambda: first_line(["go", "version"], GO_ENV), ["go"]),
    Impl("python-cbor2", "Python", "cbor2", ["python/bench.py", "python/requirements.txt"], build_python,
         lambda c, n: [sys.executable, "-s", os.path.join(HERE, "python", "bench.py"), c, str(n), FIX],
         python_version, lambda: "Python " + platform.python_version(), [python_installer],
         env={"PYTHONPATH": PYLIB}),
    Impl("zig-zbor", "Zig", "zbor", ["zig/main.zig", "zig/build.zig", "zig/build.zig.zon"], build_zig,
         lambda c, n: [os.path.join(WORK, "zig-out", "bin", "cbor-bench"), c, str(n), FIX],
         lambda: lock_version(os.path.join(HERE, "zig", "build.zig.zon"), "zbor", r"zbor/archive/refs/tags/([^/]+)\.tar"),
         lambda: "Zig " + first_line(["zig", "version"]), ["zig"], env=ZIG_ENV),
]
IMPL_NAMES = [i.name for i in IMPLS]


# The result check --------------------------------------------------------------------------------

def cbor_decode(data):
    """A small CBOR decoder for the check: each item as a tagged tuple, so that 1, 1.0 and True differ, and a map
    is equal to a map with the same entries in another order."""
    pos = 0

    def take(n):
        nonlocal pos
        if pos + n > len(data):
            raise ValueError("truncated")
        b = data[pos:pos + n]
        pos += n
        return b

    def arg(info):
        if info < 24:
            return info
        if info in (24, 25, 26, 27):
            return int.from_bytes(take(1 << (info - 24)), "big")
        if info == 31:
            return None
        raise ValueError(f"bad additional info {info}")

    def item():
        h = take(1)[0]
        major, info = h >> 5, h & 31
        if major == 7:
            if info == 20:
                return ("bool", False)
            if info == 21:
                return ("bool", True)
            if info == 22:
                return ("null",)
            if info == 23:
                return ("undefined",)
            if info == 24:
                return ("simple", take(1)[0])
            if info == 25:
                return ("float", struct.unpack(">e", take(2))[0])
            if info == 26:
                return ("float", struct.unpack(">f", take(4))[0])
            if info == 27:
                return ("float", struct.unpack(">d", take(8))[0])
            if info < 20:
                return ("simple", info)
            raise ValueError(f"bad simple value {info}")
        n = arg(info)
        if major == 0:
            return ("int", n)
        if major == 1:
            return ("int", -1 - n)
        if major in (2, 3):
            if n is None:
                b = b""
                while data[pos:pos + 1] != b"\xff":
                    c = take(1)[0]
                    if c >> 5 != major:
                        raise ValueError("bad chunk")
                    m = arg(c & 31)
                    if m is None:
                        raise ValueError("nested indefinite chunk")
                    b += take(m)
                take(1)
            else:
                b = take(n)
            return ("bytes", b) if major == 2 else ("text", b.decode("utf-8"))
        if major == 4:
            xs = []
            while (len(xs) < n) if n is not None else data[pos:pos + 1] != b"\xff":
                xs.append(item())
            if n is None:
                take(1)
            return ("array", tuple(xs))
        if major == 5:
            kvs = []
            while (len(kvs) < n) if n is not None else data[pos:pos + 1] != b"\xff":
                kvs.append((item(), item()))
            if n is None:
                take(1)
            return ("map", tuple(sorted(kvs, key=repr)))
        if major == 6:
            return ("tag", n, item())
        raise ValueError("unreachable")

    v = item()
    if pos != len(data):
        raise ValueError(f"{len(data) - pos} bytes after the item")
    return v


def expected_value(case):
    """The decoded fixture of a case, as cbor_decode gives it: the value a correct op gives back."""
    fixture, _, _ = CASES[case]
    with open(os.path.join(FIX, fixture + ".cbor"), "rb") as f:
        v = cbor_decode(f.read())
    if case == "unknown_parse":
        keep = {("text", k) for k in REC_FIELDS}
        v = ("array", tuple(("map", tuple(kv for kv in rec[1] if kv[0] in keep)) for rec in v[1]))
    return v


class Unsupported(Exception):
    pass


class Wrong(Exception):
    pass


def check_case(impl, case, timeout):
    """`<impl> <case> check`: the CBOR of the result of one op, compared with the fixture. Gives the checksum of
    one op for the timed runs."""
    cmd = impl.argv(case, "check")
    try:
        p = subprocess.run(cmd, capture_output=True, env=env_of(impl.env), cwd=ROOT, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Wrong(f"check timed out after {timeout} s")
    # The error of a program: its stderr (Roc writes it to stdout).
    err = p.stderr.decode(errors="replace").strip() or p.stdout.decode(errors="replace").strip()
    if p.returncode == 3:
        raise Unsupported(err)
    if p.returncode != 0:
        raise Wrong(f"check: exit {p.returncode}: {err[-300:]}")
    try:
        out = bytes.fromhex(p.stdout.decode().strip())
        got = cbor_decode(out)
    except ValueError as e:
        raise Wrong(f"check: the output is not one CBOR item in hex ({e})")
    if got != expected_value(case):
        raise Wrong("check: the result is not the value of the fixture")
    _, kind, count = CASES[case]
    return {"encode": len(out), "value": 1, "typed": count}[kind]


# Measurement -----------------------------------------------------------------------------------

RUSAGE = os.path.join(WORK, "rusage")


def build_rusage():
    src = os.path.join(HERE, "rusage.c")
    if not os.path.exists(RUSAGE) or os.path.getmtime(RUSAGE) < os.path.getmtime(src):
        sh([os.environ.get("CC", "cc"), "-O2", "-o", RUSAGE, src])


def run_once(impl, case, loops, per_op, timeout):
    """One run through rusage.c: (wall ns, cpu ns, max RSS KiB). Its checksum must be loops * per_op."""
    try:
        p = subprocess.run([RUSAGE] + impl.argv(case, loops), capture_output=True, env=env_of(impl.env), cwd=ROOT,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{loops} loops: timed out after {timeout} s")
    err = p.stderr.decode(errors="replace")
    m = re.search(r"rusage wall_ns=(\d+) cpu_ns=(\d+) maxrss_kib=(\d+) status=(\d+)\s*$", err)
    if not m:
        raise RuntimeError(f"{loops} loops: no rusage line: {err[-300:]}")
    wall, cpu, rss, code = map(int, m.groups())
    if code == 3:
        raise Unsupported(err[: m.start()].strip())
    if code != 0:
        raise RuntimeError(f"{loops} loops: exit {code}: {err[-300:]}")
    out = p.stdout.decode().split()
    want = (loops * per_op) % (1 << 64)
    if len(out) != 3 or out[2] != str(want):
        raise Wrong(f"{loops} loops: checksum {' '.join(out[2:]) or '(none)'}, expected {want}")
    return wall, cpu, rss


def medians(impl, case, loops, runs, per_op, timeout):
    rs = [run_once(impl, case, loops, per_op, timeout) for _ in range(runs)]
    return tuple(statistics.median(r[i] for r in rs) for i in range(3))


def find_loops(impl, case, base_ns, target_ms, per_op, timeout):
    loops = 1
    while True:
        wall, _, _ = run_once(impl, case, loops, per_op, timeout)
        t = wall - base_ns
        if t >= target_ms * 250_000 or loops >= 1_000_000:
            return max(1, int(loops * target_ms * 1e6 / max(t, 1)))
        loops *= 4


def measure_case(impl, case, runs, target_ms, timeout):
    fixture, kind, _ = CASES[case]
    try:
        per_op = check_case(impl, case, timeout)
        _, _, rss1 = medians(impl, case, 1, 3, per_op, timeout)
        wall0, cpu0, rss0 = medians(impl, case, 0, runs, per_op, timeout)
        loops = find_loops(impl, case, wall0, target_ms, per_op, timeout)
        walln, cpun, rssn = medians(impl, case, loops, runs, per_op, timeout)
    except Unsupported as e:
        return {"status": "n/a", "note": str(e)}
    except Wrong as e:
        return {"status": "wrong", "note": str(e)}
    except RuntimeError as e:
        return {"status": "error", "note": str(e)}
    ms = max(walln - wall0, 0) / loops / 1e6
    cpu_ms = max(cpun - cpu0, 0) / loops / 1e6
    size = os.path.getsize(os.path.join(FIX, fixture + ".cbor"))
    return {
        "status": "ok",
        "loops": loops,
        "ms_per_op": round(ms, 5),
        "cpu_ms_per_op": round(cpu_ms, 5),
        "mb_per_s": round(size / (ms * 1e3), 1) if ms > 0 else None,
        "base_rss_kib": rss0,
        "op_rss_kib": max(rss1 - rss0, 0),
        "peak_rss_kib": rssn,
        "start_ms": round(wall0 / 1e6, 2),
        "out_bytes": per_op if kind == "encode" else None,
    }


def measure(impl, cases, runs, target_ms, timeout, key, toolchain):
    print(f"measure {impl.name} ...", file=sys.stderr, flush=True)
    res = {}
    for c in cases:
        res[c] = measure_case(impl, c, runs, target_ms, timeout)
        r = res[c]
        info = f"{r['ms_per_op']:.3f} ms" if r["status"] == "ok" else f"{r['status']}: {r['note'][:200]}"
        print(f"  {c:<15} {info}", file=sys.stderr, flush=True)
    return {
        "impl": impl.name, "lang": impl.lang, "lib": impl.lib, "version": impl.version(),
        "toolchain": toolchain, "key": key, "cpu": cpu_model(), "date": time.strftime("%Y-%m-%d"),
        "runs": runs, "target_ms": target_ms, "cases": res,
    }


# Fixtures --------------------------------------------------------------------------------------

def write_fixtures():
    os.makedirs(FIX, exist_ok=True)
    for name in FIXTURES:
        out = sh([os.path.join(WORK, "roc-dump.bin"), "dump", name], timeout=RUN_TIMEOUT)
        with open(os.path.join(FIX, name + ".cbor"), "wb") as f:
            f.write(bytes.fromhex(out.strip()))
    return fixtures_hash()


def fixtures_hash():
    h = hashlib.sha256()
    for name in FIXTURES:
        with open(os.path.join(FIX, name + ".cbor"), "rb") as f:
            h.update(name.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return h.hexdigest()[:16]


# The report ------------------------------------------------------------------------------------

def fmt(v, spec):
    return "-" if v is None else format(v, spec)


def report(results, cases, skipped):
    order = [r for r in results]
    roc = next((r for r in order if r["impl"] == "roc"), None)
    lines = []
    lines.append("# roc-cbor against other CBOR libraries\n")
    lines.append(f"CPU: {cpu_model()}. Runs: medians of {order[0]['runs']} runs of about {order[0]['target_ms']} ms.\n")
    lines.append("| Impl | Library | Version | Toolchain | Measured | Cache key |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for r in order:
        lines.append(f"| {r['impl']} | {r['lib']} | {r['version']} | {r['toolchain']} | {r['date']} | {r['key']} |")
    lines.append("")
    if skipped:
        lines.append("Skipped: " + "; ".join(f"{n} ({why})" for n, why in skipped) + ".\n")

    def table(title, field, spec, unit, better):
        lines.append(f"## {title}\n")
        lines.append(f"{unit}. {better}. The ratio column is roc / the best of the others.\n")
        lines.append("| Case | " + " | ".join(r["impl"] for r in order) + " | roc / best other |")
        lines.append("| --- | " + " | ".join("---:" for _ in order) + " | ---: |")
        for c in cases:
            row, others = [], []
            for r in order:
                x = r["cases"].get(c, {})
                if x.get("status") != "ok":
                    row.append(x.get("status", "-"))
                    continue
                v = field(x)
                row.append(fmt(v, spec))
                if r["impl"] != "roc" and v is not None:
                    others.append(v)
            ratio = "-"
            rv = roc and roc["cases"].get(c, {})
            if rv and rv.get("status") == "ok" and others and field(rv) is not None:
                best = max(others) if better.lower().startswith("higher") else min(others)
                ratio = f"{field(rv) / best:.2f}" if best else "-"
            lines.append(f"| {c} | " + " | ".join(row) + f" | {ratio} |")
        lines.append("")

    table("Time per op", lambda x: x["ms_per_op"], ".3f", "ms per op (wall)", "Lower is better")
    table("Throughput", lambda x: x["mb_per_s"], ".0f", "MB/s of the CBOR fixture (1 MB = 1e6 bytes)",
          "Higher is better")
    table("CPU per op", lambda x: x["cpu_ms_per_op"], ".3f",
          "ms of user + sys CPU per op (above wall time: work on other threads, such as a GC)", "Lower is better")
    table("Memory of one op", lambda x: x["op_rss_kib"] / 1024, ".1f",
          "MiB: peak RSS of 1 loop - peak RSS of 0 loops", "Lower is better")
    table("Peak RSS after N loops", lambda x: x["peak_rss_kib"] / 1024, ".1f",
          "MiB: peak RSS of the timed run (runtime + input + the heap after N ops)", "Lower is better")
    table("Base RSS", lambda x: x["base_rss_kib"] / 1024, ".1f",
          "MiB: peak RSS with 0 loops (runtime + fixture + setup)", "Lower is better")
    table("Start", lambda x: x["start_ms"], ".1f", "ms: wall time of a run with 0 loops", "Lower is better")
    lines.append("## Output size of the encode cases\n")
    lines.append("| Case | " + " | ".join(r["impl"] for r in order) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in order) + " |")
    for c in cases:
        if CASES[c][1] != "encode":
            continue
        lines.append(f"| {c} | " + " | ".join(
            str(r["cases"].get(c, {}).get("out_bytes") or r["cases"].get(c, {}).get("status", "-")) for r in order) + " |")
    lines.append("")
    problems = [(r["impl"], c, r["cases"][c]) for r in order for c in cases
                if r["cases"].get(c, {}).get("status") in ("wrong", "error")]
    if problems:
        lines.append("## Failed checks and errors\n")
        lines.append("| Impl | Case | Status | Note |")
        lines.append("| --- | --- | --- | --- |")
        for impl, c, x in problems:
            note = x.get("note", "").replace("|", "\\|").replace("\n", " ")[:300]
            lines.append(f"| {impl} | {c} | {x['status']} | {note} |")
        lines.append("")
    lines.append("## Cases\n")
    lines.append("| Case | Input | Fixture bytes |")
    lines.append("| --- | --- | ---: |")
    for c in cases:
        lines.append(f"| {c} | {CASE_DOC[c]} | {os.path.getsize(os.path.join(FIX, CASES[c][0] + '.cbor'))} |")
    lines.append("")
    return "\n".join(lines)


def check_roc():
    """Stops when there is no Roc compiler; warns when it is not the nightly of roc-cbor/.roc-version."""
    if shutil.which(ROC) is None:
        raise SystemExit(f"no Roc compiler `{ROC}`: set ROC=/path/to/roc (the nightly of roc-cbor/.roc-version). "
                         "The fixtures come from roc-cbor/bench/bench.roc, so each run needs it.")
    try:
        with open(os.path.join(ROC_CBOR, ".roc-version")) as f:
            want = f.read().strip()
    except OSError:
        return
    have = first_line([ROC, "version"])
    if want.removeprefix("nightly-") not in have:
        print(f"warning: `{ROC} version` is {have!r}, but roc-cbor/.roc-version is {want}", file=sys.stderr)


def main():
    global RUN_TIMEOUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="measure the other languages again (ignore the cache)")
    ap.add_argument("--only", nargs="*", help=f"implementations to run (default: all): {' '.join(IMPL_NAMES)}")
    ap.add_argument("--cases", nargs="*", default=list(CASES), help="cases (default: all)")
    ap.add_argument("--runs", type=int, default=int(os.environ.get("COMPARE_RUNS", 5)))
    ap.add_argument("--ms", type=int, default=int(os.environ.get("COMPARE_MS", 300)), help="target ms of a timed run")
    ap.add_argument("--timeout", type=int, default=RUN_TIMEOUT, help="seconds a run of a benchmark may take")
    ap.add_argument("--no-build", action="store_true", help="use the Roc binary and the fixtures of the last run")
    a = ap.parse_args()
    RUN_TIMEOUT = a.timeout
    for c in a.cases:
        if c not in CASES:
            raise SystemExit(f"unknown case: {c} (cases: {' '.join(CASES)})")
    for n in a.only or []:
        if n not in IMPL_NAMES:
            raise SystemExit(f"unknown implementation: {n} (implementations: {' '.join(IMPL_NAMES)})")
    if not a.cases:
        raise SystemExit("--cases needs at least one case")
    if not os.path.exists(os.path.join(ROC_CBOR, "bench", "bench.roc")):
        raise SystemExit("roc-cbor/ is empty: run `git submodule update --init` (or clone with --recursive)")
    impls = [i for i in IMPLS if not a.only or i.name in a.only]
    if not impls:
        raise SystemExit("--only needs at least one implementation")
    for d in (WORK, RESULTS, TMP):
        os.makedirs(d, exist_ok=True)

    try:
        build_rusage()
    except BuildError as e:
        raise SystemExit(f"cannot build rusage.c (needs a C compiler, or CC): {e}")
    roc_bin = os.path.join(WORK, "roc-compare.bin")
    have_fixtures = all(os.path.exists(os.path.join(FIX, n + ".cbor")) for n in FIXTURES)
    if a.no_build:
        if not have_fixtures or (any(i.name == "roc" for i in impls) and not os.path.exists(roc_bin)):
            raise SystemExit("--no-build: no fixtures or Roc binary of a last run in .cache/ (run once without it)")
        fixtures_sha = fixtures_hash()
    else:
        check_roc()
        print("build roc (fixtures, then the cases) ...", file=sys.stderr, flush=True)
        try:
            build_roc_dump()
            fixtures_sha = write_fixtures()
            if any(i.name == "roc" for i in impls):
                build_roc()
        except BuildError as e:
            raise SystemExit(f"roc: {e}")
    cpu = cpu_model()

    results, skipped = [], []
    for impl in impls:
        missing = impl.missing()
        if missing:
            why = "; ".join(missing)
            print(f"skip {impl.name}: {why}", file=sys.stderr)
            skipped.append((impl.name, why))
            continue
        toolchain = impl.toolchain()
        if impl.name == "roc":
            key = fixtures_sha
            r = measure(impl, a.cases, a.runs, a.ms, a.timeout, key, toolchain)
            with open(os.path.join(WORK, "roc.json"), "w") as f:
                json.dump(r, f, indent=1)
            results.append(r)
            continue
        key = impl.key(fixtures_sha, cpu, toolchain)
        path = os.path.join(RESULTS, impl.name + ".json")
        cached = None
        if os.path.exists(path) and not a.refresh:
            with open(path) as f:
                cached = json.load(f)
            if cached.get("key") != key or any(c not in cached["cases"] for c in a.cases):
                why = "key" if cached.get("key") != key else "cases"
                print(f"{impl.name}: cache is stale ({why}), measure again", file=sys.stderr)
                cached = None
        if cached:
            print(f"{impl.name}: cached ({cached['date']}, key {key})", file=sys.stderr)
            results.append(cached)
            continue
        print(f"build {impl.name} ...", file=sys.stderr, flush=True)
        try:
            impl.build()
        except BuildError as e:
            print(f"skip {impl.name}: the build failed:\n{e}", file=sys.stderr)
            skipped.append((impl.name, "the build failed"))
            continue
        # Cached results hold every case, so that a later run with other --cases can use them.
        r = measure(impl, list(CASES), a.runs, a.ms, a.timeout, key, toolchain)
        with open(path, "w") as f:
            json.dump(r, f, indent=1, sort_keys=True)
            f.write("\n")
        results.append(r)

    if not results:
        raise SystemExit("no implementation was measured: " + "; ".join(f"{n} ({w})" for n, w in skipped))
    text = report(results, a.cases, skipped)
    with open(os.path.join(WORK, "report.md"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    sys.setrecursionlimit(10_000)
    main()
