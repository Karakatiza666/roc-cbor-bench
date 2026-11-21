app [] { pkg: "../roc-cbor/src/main.roc" }

# The Roc side of roc-cbor-bench. Usage: compare <case> <loops>, or compare <case> check
# The inputs are the fixtures of run.py (written by roc-cbor/bench/bench.roc `dump`), embedded at
# build time, so that Roc parses the same bytes as the other languages and builds no input at setup.
# The setup of an encode case parses the fixture into the value to encode (the same as the others).
# The output is "case loops checksum"; the contract of each case is in README.md. A failed parse adds `fail`
# (2^40) to the checksum, so that it cannot pass the check of run.py. `check` prints the CBOR of the result of one
# op as hex (the parsed value encoded again, or the output of an encode), and run.py compares it with the fixture.

import pkg.Cbor
import "../.cache/fixtures/bytes.cbor" as bytes_cbor_c : List(U8)
import "../.cache/fixtures/records.cbor" as records_cbor_c : List(U8)
import "../.cache/fixtures/wide.cbor" as wide_cbor_c : List(U8)
import "../.cache/fixtures/dict.cbor" as dict_cbor_c : List(U8)
import "../.cache/fixtures/texts.cbor" as texts_cbor_c : List(U8)
import "../.cache/fixtures/deep.cbor" as deep_cbor_c : List(U8)

Rec : { name : Str, id : U64, ok : Bool, score : F64, tags : List(Str) }

## Runs `work` `loops` times and sums its results.
repeat : U64, (U64 -> U64) -> U64
repeat = |loops, work| {
	var $sum = 0
	for i in (0.U64).until(loops) {
		$sum = $sum.plus_wrap(work(i))
	}
	$sum
}

## What a failed parse adds to the checksum (the same in each language).
fail : U64
fail = 1_099_511_627_776

parse_len : Try(List(a), _) -> U64
parse_len = |r|
	match r {
		Ok(l) => l.len()
		Err(_) => fail
	}

is_ok : Try(a, _) -> U64
is_ok = |r| if (r.is_ok()) 1 else fail

## The fixture, through a value the compiler cannot know (seed is 0 at run time). Without it, the
## compiler evaluates a parse of a constant fixture at build time.
at_runtime : U64, List(U8) -> List(U8)
at_runtime = |seed, l| if (seed > 1000) [] else l

run : Str, U64, U64 -> Try(U64, [UnknownCase, BadFixture])
run = |case, loops, seed| {
	bytes_cbor = at_runtime(seed, bytes_cbor_c)
	records_cbor = at_runtime(seed, records_cbor_c)
	wide_cbor = at_runtime(seed, wide_cbor_c)
	dict_cbor = at_runtime(seed, dict_cbor_c)
	texts_cbor = at_runtime(seed, texts_cbor_c)
	deep_cbor = at_runtime(seed, deep_cbor_c)
	match case {
		"bytes_encode" => {
			b : Cbor.Bytes
			b = Cbor.parse(bytes_cbor) ? |_| BadFixture
			Ok(repeat(loops, |_| Cbor.encode(b).len()))
		}
		"bytes_parse" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(Cbor.Bytes, _)
						r = Cbor.parse(bytes_cbor)
						r.map_ok(|b| b.to_list().len()) ?? fail
					},
				),
			)
		"records_encode" => {
			rs : List(Rec)
			rs = Cbor.parse(records_cbor) ? |_| BadFixture
			Ok(repeat(loops, |_| Cbor.encode(rs).len()))
		}
		"records_parse" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(List(Rec), _)
						r = Cbor.parse(records_cbor)
						parse_len(r)
					},
				),
			)
		"records_value" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(Cbor.Value, _)
						r = Cbor.parse(records_cbor)
						is_ok(r)
					},
				),
			)
		"unknown_parse" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(List(Rec), _)
						r = Cbor.parse(wide_cbor)
						parse_len(r)
					},
				),
			)
		"dict_parse" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(Dict(Str, U64), _)
						r = Cbor.parse(dict_cbor)
						r.map_ok(|d| d.len()) ?? fail
					},
				),
			)
		"map_value" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(Cbor.Value, _)
						r = Cbor.parse(dict_cbor)
						is_ok(r)
					},
				),
			)
		"text_parse" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(List(Str), _)
						r = Cbor.parse(texts_cbor)
						parse_len(r)
					},
				),
			)
		"deep_encode" => {
			v : Cbor.Value
			v = Cbor.parse(deep_cbor) ? |_| BadFixture
			Ok(repeat(loops, |_| Cbor.encode(v).len()))
		}
		"deep_parse" =>
			Ok(
				repeat(
					loops,
					|_| {
						r : Try(Cbor.Value, _)
						r = Cbor.parse(deep_cbor)
						is_ok(r)
					},
				),
			)
		_ => Err(UnknownCase)
	}
}

## The CBOR of the result of one op of a case: the parsed value encoded again, or the output of an encode.
check : Str, U64 -> Try(List(U8), [UnknownCase, ParseFailed])
check = |case, seed| {
	bytes_cbor = at_runtime(seed, bytes_cbor_c)
	records_cbor = at_runtime(seed, records_cbor_c)
	wide_cbor = at_runtime(seed, wide_cbor_c)
	dict_cbor = at_runtime(seed, dict_cbor_c)
	texts_cbor = at_runtime(seed, texts_cbor_c)
	deep_cbor = at_runtime(seed, deep_cbor_c)
	match case {
		"bytes_encode" | "bytes_parse" => {
			b : Cbor.Bytes
			b = Cbor.parse(bytes_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(b))
		}
		"records_encode" | "records_parse" => {
			rs : List(Rec)
			rs = Cbor.parse(records_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(rs))
		}
		"unknown_parse" => {
			rs : List(Rec)
			rs = Cbor.parse(wide_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(rs))
		}
		"records_value" => {
			v : Cbor.Value
			v = Cbor.parse(records_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(v))
		}
		"dict_parse" => {
			d : Dict(Str, U64)
			d = Cbor.parse(dict_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(d))
		}
		"map_value" => {
			v : Cbor.Value
			v = Cbor.parse(dict_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(v))
		}
		"text_parse" => {
			t : List(Str)
			t = Cbor.parse(texts_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(t))
		}
		"deep_encode" | "deep_parse" => {
			v : Cbor.Value
			v = Cbor.parse(deep_cbor) ? |_| ParseFailed
			Ok(Cbor.encode(v))
		}
		_ => Err(UnknownCase)
	}
}

## Lower-case hex of the bytes (as `bench/bench.roc dump` of roc-cbor).
to_hex : List(U8) -> Str
to_hex = |bytes| {
	digit = |n| if (n < 10) n + 0x30 else n + 0x57
	out = bytes.fold(List.with_capacity(bytes.len() * 2), |acc, b| acc.append(digit(b.shr_zf_wrap(4))).append(digit(b.bitwise_and(0xf))))
	Str.from_utf8(out) ?? ""
}

main! = |args| {
	case = args.get(0) ?? ""
	if args.get(1) == Ok("check") {
		match check(case, args.len() // 8) {
			Ok(out) => {
				echo!("${to_hex(out)}\n")
				return Ok({})
			}
			Err(UnknownCase) => {
				echo!("unknown case: ${case}\n")
				return Err(Exit(2))
			}
			Err(ParseFailed) => {
				echo!("${case}: the parse failed\n")
				return Err(Exit(4))
			}
		}
	}
	loops = U64.from_str(args.get(1) ?? "0") ?? 0
	# The seed is 0 (2 args), but the compiler cannot know it.
	seed = args.len() // 8
	match run(case, loops, seed) {
		Ok(sum) => {
			echo!("${case} ${loops.to_str()} ${sum.to_str()}\n")
			Ok({})
		}
		Err(_) => {
			echo!("unknown case or bad fixture: ${case}\n")
			Err(Exit(2))
		}
	}
}
