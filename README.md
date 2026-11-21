# roc-cbor-bench

Cross-language CBOR benchmarks for [roc-cbor](https://github.com/Karakatiza666/roc-cbor): roc-cbor and CBOR
libraries of Rust, Go, Python and Zig read and write the same fixtures.

The harness was written by Luke Boswell in
[roc-cbor PR #2](https://github.com/Karakatiza666/roc-cbor/pull/2) (its `bench/compare/` directory). It moved
here, as it was, so that bench changes do not wait for the CI of roc-cbor, and roc-cbor needs no Rust, Go or
Zig toolchain. The `dump` mode of `bench/bench.roc` that writes the fixtures stays in roc-cbor
([PR #4](https://github.com/Karakatiza666/roc-cbor/pull/4)).

License: [UPL 1.0](LICENSE), the same as roc-cbor.

## roc-cbor is a git submodule

`roc-cbor/` is a git submodule. Its commit is the roc-cbor that is compared: the fixtures come from its
`bench/bench.roc dump`, and `roc/compare.roc` imports its `src/`. Clone with the submodule:

```sh
git clone --recursive https://github.com/Karakatiza666/roc-cbor-bench.git
# or, in a clone without it:
git submodule update --init
```

To compare another roc-cbor commit, check it out in `roc-cbor/` (or bump the submodule and commit it).

The submodule points at the head commit of roc-cbor PR #4 (branch `bench-dump`), since the `dump` mode is not
on roc-cbor `main` yet. When PR #4 is merged, bump the submodule to `main`.

The Roc compiler must be the nightly that `roc-cbor/.roc-version` names. Give its path in `ROC` (default:
`roc` on `PATH`). `run.py` warns when `roc version` is another nightly.

## Usage

A diagnostic for development: roc-cbor and CBOR libraries of other languages read and write the same fixtures,
so that a slow path or a compiler issue shows up (time per op, throughput, CPU, peak RSS). The other libraries are
reference points, not a contest, and no result is committed.

```sh
ROC=/path/to/roc python3 run.py              # measure Roc, reuse the cached results of the others
ROC=/path/to/roc python3 run.py --refresh    # measure all again
ROC=/path/to/roc python3 run.py --only roc go-fxamacker --cases records_parse dict_parse --runs 9
```

The report is printed and written to `.cache/report.md`. Each run needs `ROC` (the fixtures come from
roc-cbor) and a C compiler (`cc`, or `CC`) for `rusage.c`. The other toolchains are needed only for their
implementation; one that is missing, or whose build fails, is skipped with a message:

| Impl | Needs |
| --- | --- |
| `rust-*` | `cargo` and `rustc` (`cargo build --locked`) |
| `go-fxamacker` | `go` 1.26.5 or later. `run.py` sets `GOTOOLCHAIN=local`, so an older `go` fails instead of downloading a toolchain. |
| `python-cbor2` | `pip` of the Python that runs `run.py`, or `uv`. cbor2 is installed from its wheels with `--require-hashes --only-binary :all:` (no sdist build, no pip cache) into `.cache/pylib`, again when `python/requirements.txt` changes. |
| `zig-zbor` | `zig` 0.17.1 or later |

Downloads (crates, Go modules, the cbor2 wheel, zbor) are pinned by `rust/Cargo.lock`, `go/go.sum`,
`python/requirements.txt` (sha256) and `zig/build.zig.zon` (hash). The Go module and build caches, the Zig
cache, the Roc cache and temporary files (`TMPDIR`) are under `.cache/`. Each build may take
30 minutes and each run of a benchmark `--timeout` seconds (default 120).

Peak RSS comes from `ru_maxrss`: KiB on Linux, bytes on macOS (`rusage.c` scales it). The harness is tested
on Linux.

## Implementations

| Impl | Library | Typed read and write | Dynamic value | Source |
| --- | --- | --- | --- | --- |
| `roc` | roc-cbor (the submodule) | derived from the Roc type | `Cbor.Value` | `roc/compare.roc` |
| `rust-ciborium` | [ciborium](https://crates.io/crates/ciborium) 0.2 | serde derive | `ciborium::Value` | `rust/src/main.rs` |
| `rust-cbor4ii` | [cbor4ii](https://crates.io/crates/cbor4ii) 1.2 | serde derive | `cbor4ii::core::Value` | `rust/src/main.rs` |
| `go-fxamacker` | [fxamacker/cbor](https://github.com/fxamacker/cbor) v2 | struct tags (reflection) | `interface{}` | `go/main.go` |
| `python-cbor2` | [cbor2](https://pypi.org/project/cbor2/) 6 (compiled) | `loads` + a slotted class for each record | `loads` | `python/bench.py` |
| `zig-zbor` | [zbor](https://github.com/r4gus/zbor) 0.23 | comptime reflection | none (n/a) | `zig/main.zig` |

Versions are pinned by `rust/Cargo.lock`, `go/go.sum`, `python/requirements.txt` and `zig/build.zig.zon`.

## Method

1. `roc-cbor/bench/bench.roc dump <fixture>` writes each input as hex. `run.py` writes them to `.cache/fixtures/`.
   Each implementation parses these bytes, so each side reads the same input (Roc embeds them with
   `import "…" as x : List(U8)` in `roc/compare.roc`; the others read the file at start).
2. Each implementation is a program `<impl> <case> <loops> [fixture_dir]` that prints `case loops checksum`.
   The setup (the fixture; for an encode case, the parse of the fixture into the value to encode) is not
   timed. Exit 3 means the library cannot do the case (n/a). Exit 4 means the setup parse failed.
   `<impl> <case> check [fixture_dir]` prints the CBOR of the result of one op as hex: the parsed value
   encoded again with the same library, or the output of one encode.
3. Each run goes through `rusage.c` (fork, exec, `wait4`): wall time, user + sys CPU, peak RSS.
   Not `os.wait4` from Python: Linux keeps the peak RSS of the forked parent in the child across `exec`.
4. The check: `run.py` decodes the output of `check` and compares it with the decoded fixture (for
   `unknown_parse`, each record with its 5 fields). Maps compare with their entries in any order, and
   1, 1.0 and `true` are different values. A float of any width is equal to the same number.
   Then each run must print `loops` x the checksum of one op: the item count of a typed parse, 1 for a
   dynamic value, the output length of an encode. A failed parse adds 2^40 to the checksum, so it cannot
   pass. A case that fails a check is `wrong` in the report (with the reason) and is not timed.
5. The timed runs: the median of 3 runs with 1 loop (memory of one op), the median of `--runs` runs with
   0 loops, a loop count for about `--ms` of work, and the median of `--runs` runs with that count.

| Metric | Definition |
| --- | --- |
| ms per op | (median wall of N loops - median wall of 0 loops) / N |
| MB/s | fixture bytes / ms per op (1 MB = 10^6 bytes; for an encode case, the bytes of the fixture) |
| CPU per op | the same with user + sys CPU. Above ms per op: work on other threads (the GC of Go). |
| Memory of one op | peak RSS of 1 loop - peak RSS of 0 loops: the memory of one parse or encode at its peak. For Roc it also counts the fixture (embedded in the binary, so its pages come in when the op reads them; the others read the file at start): up to 1.4 MiB. |
| Peak RSS after N loops | the peak RSS of the timed run: growth above one op is a GC heap or a leak |
| Base RSS, Start | the peak RSS and the wall time of a run with 0 loops: runtime and setup |

## Cases

| Case | Fixture | What |
| --- | --- | --- |
| `bytes_encode`, `bytes_parse` | `bytes` | a byte string of 1 MB |
| `records_encode`, `records_parse` | `records` | 10k records `{id, name, ok, score, tags}`, typed |
| `records_value` | `records` | the same into the dynamic value |
| `unknown_parse` | `wide` | 10k records with 12 more fields each, read as the 5-field record (skip unknown fields) |
| `dict_parse` | `dict` | a map of 100k text keys into a hash map of text to integer |
| `map_value` | `dict` | the same into the dynamic value |
| `text_parse` | `texts` | 10k texts of multi-byte UTF-8 into a list of strings |
| `deep_encode`, `deep_parse` | `deep` | 500 nested arrays, dynamic value |

## Caching

`.cache/results/<impl>.json` holds the last measurement of each other language (local, not committed),
so that a later run of `run.py` measures only Roc. A cached result is used when its `key` matches: a hash of the
fixtures, of the source and lock files of the implementation, of its toolchain version (`rustc --version`,
`go version`, the Python version, `zig version`) and of the CPU model. A change of the fixtures (a change of
`roc-cbor/bench/bench.roc` or of the writer), of the code or toolchain of that language, or another CPU
measures it again. Roc writes its result to `.cache/roc.json`. Cached numbers are of one machine
(the `cpu` field): compare Roc with them only on that machine, or run `--refresh`.

## Differences between the libraries

These hold for the inputs here. They are the reasons a column is not the same work as another column.

| Difference | Effect |
| --- | --- |
| roc-cbor checks each item before the typed parse (well-formed, UTF-8, shortest heads, no duplicate keys, depth) | Work the others do in part. None checks shortest heads. Go (`DupMapKeyQuiet`) and cbor2 allow duplicate keys by default. zbor checks well-formedness, but not the UTF-8 of a text it reads. ciborium, cbor4ii, Go and cbor2 check UTF-8. |
| `Cbor.Bytes` of `bytes_parse` is a slice of the input (no copy) | Roc does almost no work; the others copy 1 MB. |
| Python has no typed reader | `records_parse` is `loads` and a slotted object for each record; `dict_parse` and `text_parse` are `loads`. |
| zbor has no dynamic value tree (its `DataItem` is a checked view of the bytes), no hash map target, and a fixed depth limit of 128 | `*_value`, `deep_*`: n/a. `dict_parse` walks zbor's map iterator into a `std.StringHashMap`. Each parse uses an arena that is freed after the op. |
| Depth limits | Raised so that `deep` (500) reads: ciborium 1024 (default 256), cbor4ii 1024 (a reader with its own limit; the default is 256), Go `MaxNestedLevels` 1024 (default 32), cbor2 `max_depth` 1024 (default 400). roc-cbor: 512. |
| cbor4ii reads an `f64` only from an 8-byte float | A record with the f16 `score` of preferred serialization fails. `Rec.score` uses `deserialize_with` that takes each float width. |
| Float width on write | roc-cbor, ciborium and Go (`ShortestFloat16`) write the shortest float (474,635 bytes for `records_encode`). cbor4ii, cbor2 and zbor write 8-byte floats (518,613 bytes). |
| Garbage collection | Go and Python free with a GC; their CPU per op can be above the wall time, and their peak RSS grows with N. |
| Map key order on write | No library sorts map keys here. roc-cbor writes the fields of a record in the order of their names (`id, name, ok, score, tags`), and a `Dict` or a `Cbor.Value` map in its own order (`key_order: Given`, the default; `Sorted`, the order of RFC 8949 4.2.1, is an option that is not used here). serde (ciborium, cbor4ii) and zbor write the order of the struct fields, the same order for `Rec`. Go writes struct fields in their order, and a Go map in a random order (`SortNone`, the default). cbor2 writes a dict in its insertion order. The check compares maps in any order. |
| The input in memory | Roc embeds the fixtures in its binary, and its pages come in when an op reads them. The others read the fixture file at start, so it is in their base RSS. So Base RSS and Memory of one op are not like for like: Roc's base RSS is lower, and its memory of one op is higher by up to the fixture size (1.4 MiB). |
| Strings of a parse | roc-cbor's `Cbor.Bytes` is a slice of the input. Each other target copies each byte string and text: serde `String`, `ByteBuf` and `Value`, Go `string` and `[]byte`, cbor2, and zbor (`parse` of a `[]const u8` allocates a copy in the arena; it does not borrow from the input). So `records_*`, `text_parse` and `dict_parse` of zbor do the same copying as the others. |
