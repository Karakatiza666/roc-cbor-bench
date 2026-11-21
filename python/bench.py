"""The Python side of roc-cbor-bench. Usage: python -s bench.py <case> <loops | check> <fixture_dir>
(run.py runs it with -s and PYTHONPATH=.cache/pylib, where cbor2 is installed).

Library: cbor2 (its compiled decoder and encoder). Python has no typed reader: a "typed" parse case is
cbor2.loads plus the build of the Python objects a program would use (a slotted Rec for each record).
The contract of each case is in README.md. A failed parse adds FAIL to the checksum. `check` prints the CBOR
of the result of one op as hex (the parsed value encoded again, or the output of an encode); run.py compares
it with the fixture.
"""

import sys

import cbor2


# What a failed parse adds to the checksum (the same in each language).
FAIL = 1 << 40


class Rec:
    __slots__ = ("id", "name", "ok", "score", "tags")

    def __init__(self, d):
        self.id = d["id"]
        self.name = d["name"]
        self.ok = d["ok"]
        self.score = d["score"]
        self.tags = d["tags"]

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


FIXTURE = {
    "bytes_encode": "bytes", "bytes_parse": "bytes",
    "records_encode": "records", "records_parse": "records", "records_value": "records",
    "unknown_parse": "wide", "dict_parse": "dict", "map_value": "dict", "text_parse": "texts",
    "deep_encode": "deep", "deep_parse": "deep",
}


def main():
    case, loops, fixture_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    if case not in FIXTURE:
        print(f"unknown case: {case}", file=sys.stderr)
        sys.exit(2)
    with open(f"{fixture_dir}/{FIXTURE[case]}.cbor", "rb") as f:
        data = f.read()
    dumps = cbor2.dumps

    def loads(b):
        # max_depth: the default is 400; `deep` has 500 levels (the limit of roc-cbor is 512).
        return cbor2.loads(b, max_depth=1024)

    if loops == "check":
        try:
            if case in ("records_encode", "records_parse", "unknown_parse"):
                out = dumps([Rec(d).to_dict() for d in loads(data)])
            else:
                out = dumps(loads(data))
        except Exception as e:  # a failed parse of the fixture
            print(f"{case}: {e!r}", file=sys.stderr)
            sys.exit(4)
        print(out.hex())
        return
    loops = int(loops)

    def ok_len(f):
        try:
            return len(f())
        except Exception:
            return FAIL

    total = 0
    if case in ("bytes_encode", "records_encode", "deep_encode"):
        try:
            value = loads(data)
        except Exception as e:
            print(f"{case}: the parse of the fixture failed: {e!r}", file=sys.stderr)
            sys.exit(4)
        for _ in range(loops):
            total += len(dumps(value))
    elif case in ("records_parse", "unknown_parse"):
        for _ in range(loops):
            total += ok_len(lambda: [Rec(d) for d in loads(data)])
    elif case in ("bytes_parse", "dict_parse", "text_parse"):
        for _ in range(loops):
            total += ok_len(lambda: loads(data))
    else:  # records_value, map_value, deep_parse: the dynamic value
        for _ in range(loops):
            try:
                loads(data)
                total += 1
            except Exception:
                total += FAIL
    print(f"{case} {loops} {total}")


if __name__ == "__main__":
    sys.setrecursionlimit(10_000)
    main()
