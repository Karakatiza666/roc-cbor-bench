// The Go side of roc-cbor-bench. Usage: cbor-bench <case> <loops | check> <fixture_dir>
// Library: github.com/fxamacker/cbor/v2. The contract of each case is in README.md.
// A failed parse adds fail to the checksum. `check` prints the CBOR of the result of one op as hex (the
// parsed value encoded again, or the output of an encode); run.py compares it with the fixture.
package main

import (
	"encoding/hex"
	"fmt"
	"os"
	"strconv"

	"github.com/fxamacker/cbor/v2"
)

type Rec struct {
	ID    uint64   `cbor:"id"`
	Name  string   `cbor:"name"`
	Ok    bool     `cbor:"ok"`
	Score float64  `cbor:"score"`
	Tags  []string `cbor:"tags"`
}

var sink uint64

// What a failed parse adds to the checksum (the same in each language).
const fail = 1 << 40

func die(code int, msg ...interface{}) {
	fmt.Fprintln(os.Stderr, msg...)
	os.Exit(code)
}

func fixtureOf(c string) string {
	switch c {
	case "bytes_encode", "bytes_parse":
		return "bytes"
	case "records_encode", "records_parse", "records_value":
		return "records"
	case "unknown_parse":
		return "wide"
	case "dict_parse", "map_value":
		return "dict"
	case "text_parse":
		return "texts"
	case "deep_encode", "deep_parse":
		return "deep"
	}
	fmt.Fprintln(os.Stderr, "unknown case:", c)
	os.Exit(2)
	return ""
}

func main() {
	if len(os.Args) != 4 {
		fmt.Fprintln(os.Stderr, "usage: cbor-bench <case> <loops | check> <fixture_dir>")
		os.Exit(2)
	}
	c := os.Args[1]
	data, err := os.ReadFile(os.Args[3] + "/" + fixtureOf(c) + ".cbor")
	if err != nil {
		die(2, "read fixture:", err)
	}
	// Preferred serialization (shortest floats), and the depth of `deep` (500 > the default 32).
	enc, err := cbor.EncOptions{ShortestFloat: cbor.ShortestFloat16}.EncMode()
	if err != nil {
		die(2, "EncMode:", err)
	}
	dec, err := cbor.DecOptions{MaxNestedLevels: 1024}.DecMode()
	if err != nil {
		die(2, "DecMode:", err)
	}
	// The setup of a case: a parse that must work.
	setup := func(v interface{}) {
		if err := dec.Unmarshal(data, v); err != nil {
			die(4, "the parse of the fixture failed:", err)
		}
	}
	marshal := func(v interface{}) []byte {
		out, err := enc.Marshal(v)
		if err != nil {
			die(4, "encode:", err)
		}
		return out
	}
	if os.Args[2] == "check" {
		// The CBOR of the result of one op: the parsed value encoded again, or the output of an encode.
		var v interface{}
		switch c {
		case "bytes_encode", "bytes_parse":
			var b []byte
			setup(&b)
			v = b
		case "records_encode", "records_parse", "unknown_parse":
			var rs []Rec
			setup(&rs)
			v = rs
		case "dict_parse":
			var d map[string]uint64
			setup(&d)
			v = d
		case "text_parse":
			var t []string
			setup(&t)
			v = t
		default: // records_value, map_value, deep_parse, deep_encode: the dynamic value
			setup(&v)
		}
		fmt.Println(hex.EncodeToString(marshal(v)))
		return
	}
	loops, err := strconv.ParseUint(os.Args[2], 10, 64)
	if err != nil {
		die(2, "bad loop count:", os.Args[2])
	}
	repeat := func(work func() uint64) uint64 {
		var sum uint64
		for i := uint64(0); i < loops; i++ {
			sum += work()
		}
		return sum
	}
	parseLen := func(v interface{}, n func() int) uint64 {
		if dec.Unmarshal(data, v) != nil {
			return fail
		}
		return uint64(n())
	}
	var sum uint64
	switch c {
	case "bytes_encode":
		var b []byte
		setup(&b)
		sum = repeat(func() uint64 { out, _ := enc.Marshal(b); return uint64(len(out)) })
	case "bytes_parse":
		sum = repeat(func() uint64 { var b []byte; return parseLen(&b, func() int { return len(b) }) })
	case "records_encode":
		var rs []Rec
		setup(&rs)
		sum = repeat(func() uint64 { out, _ := enc.Marshal(rs); return uint64(len(out)) })
	case "records_parse", "unknown_parse":
		sum = repeat(func() uint64 { var rs []Rec; return parseLen(&rs, func() int { return len(rs) }) })
	case "dict_parse":
		sum = repeat(func() uint64 { var d map[string]uint64; return parseLen(&d, func() int { return len(d) }) })
	case "text_parse":
		sum = repeat(func() uint64 { var t []string; return parseLen(&t, func() int { return len(t) }) })
	case "records_value", "map_value", "deep_parse":
		// The dynamic value: interface{} (maps decode as map[interface{}]interface{}).
		sum = repeat(func() uint64 { var v interface{}; return parseLen(&v, func() int { return 1 }) })
	case "deep_encode":
		var v interface{}
		setup(&v)
		sum = repeat(func() uint64 { out, _ := enc.Marshal(v); return uint64(len(out)) })
	}
	sink = sum
	fmt.Printf("%s %d %d\n", c, loops, sum)
}
