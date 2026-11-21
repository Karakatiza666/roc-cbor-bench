//! The Rust side of roc-cbor-bench. Usage: cbor-bench <lib> <case> <loops | check> <fixture_dir>
//! lib: ciborium | cbor4ii. The contract of each case is in README.md.
//! The input of each case is a fixture written by the Roc bench (the same bytes for each language).
//! Setup (read the fixture, decode the value of an encode case) is not timed: the runner subtracts a
//! run with 0 loops. The output is "case loops checksum", so that the work is not removed. A failed parse adds
//! FAIL to the checksum. `check` prints the CBOR of the result of one op as hex (the parsed value encoded
//! again, or the output of an encode); run.py compares it with the fixture.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::hint::black_box;

#[derive(Serialize, Deserialize)]
struct Rec {
    id: u64,
    name: String,
    ok: bool,
    // cbor4ii reads an f64 only from an 8-byte float head (a mismatch for the f16 of preferred
    // serialization); deserialize_any takes each float width. ciborium does this on its own.
    #[serde(deserialize_with = "any_f64")]
    score: f64,
    tags: Vec<String>,
}

fn any_f64<'de, D: serde::Deserializer<'de>>(d: D) -> Result<f64, D::Error> {
    struct V;
    impl serde::de::Visitor<'_> for V {
        type Value = f64;
        fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
            f.write_str("a number")
        }
        fn visit_f64<E>(self, v: f64) -> Result<f64, E> {
            Ok(v)
        }
        fn visit_f32<E>(self, v: f32) -> Result<f64, E> {
            Ok(v as f64)
        }
        fn visit_u64<E>(self, v: u64) -> Result<f64, E> {
            Ok(v as f64)
        }
        fn visit_i64<E>(self, v: i64) -> Result<f64, E> {
            Ok(v as f64)
        }
    }
    d.deserialize_any(V)
}

type Blob = serde_bytes::ByteBuf;

fn fixture(dir: &str, name: &str) -> Vec<u8> {
    std::fs::read(format!("{dir}/{name}.cbor")).expect("read fixture")
}

fn fixture_of(case: &str) -> &'static str {
    match case {
        "bytes_encode" | "bytes_parse" => "bytes",
        "records_encode" | "records_parse" | "records_value" => "records",
        "unknown_parse" => "wide",
        "dict_parse" | "map_value" => "dict",
        "text_parse" => "texts",
        "deep_encode" | "deep_parse" => "deep",
        _ => {
            eprintln!("unknown case: {case}");
            std::process::exit(2)
        }
    }
}

trait Lib {
    fn de<T: serde::de::DeserializeOwned>(b: &[u8]) -> Option<T>;
    fn ser<T: Serialize>(v: &T) -> Vec<u8>;
    type Value;
    fn de_value(b: &[u8]) -> Option<Self::Value>;
    fn ser_value(v: &Self::Value) -> Vec<u8>;
}

struct Ciborium;
impl Lib for Ciborium {
    fn de<T: serde::de::DeserializeOwned>(b: &[u8]) -> Option<T> {
        ciborium::de::from_reader_with_recursion_limit(b, 1024).ok()
    }
    fn ser<T: Serialize>(v: &T) -> Vec<u8> {
        let mut out = Vec::new();
        ciborium::ser::into_writer(v, &mut out).unwrap();
        out
    }
    type Value = ciborium::Value;
    fn de_value(b: &[u8]) -> Option<Self::Value> {
        Self::de(b)
    }
    fn ser_value(v: &Self::Value) -> Vec<u8> {
        Self::ser(v)
    }
}

struct Cbor4ii;
impl Lib for Cbor4ii {
    fn de<T: serde::de::DeserializeOwned>(b: &[u8]) -> Option<T> {
        cbor4ii::serde::from_slice(b).ok()
    }
    fn ser<T: Serialize>(v: &T) -> Vec<u8> {
        cbor4ii::serde::to_vec(Vec::new(), v).unwrap()
    }
    type Value = cbor4ii::core::Value;
    fn de_value(b: &[u8]) -> Option<Self::Value> {
        use cbor4ii::core::dec::Decode;
        let mut r = DeepReader { buf: b, limit: 1024 };
        cbor4ii::core::Value::decode(&mut r).ok()
    }
    fn ser_value(v: &Self::Value) -> Vec<u8> {
        use cbor4ii::core::enc::Encode;
        let mut w = cbor4ii::core::utils::BufWriter::new(Vec::new());
        v.encode(&mut w).unwrap();
        w.into_inner()
    }
}

/// cbor4ii's SliceReader with a depth limit of 1024 (its own is 256), so that `deep` (500) reads.
struct DeepReader<'a> {
    buf: &'a [u8],
    limit: usize,
}

impl<'de> cbor4ii::core::dec::Read<'de> for DeepReader<'de> {
    type Error = cbor4ii::core::error::Never;
    #[inline]
    fn fill<'b>(&'b mut self, want: usize) -> Result<cbor4ii::core::dec::Reference<'de, 'b>, Self::Error> {
        let len = want.min(self.buf.len());
        Ok(cbor4ii::core::dec::Reference::Long(&self.buf[..len]))
    }
    #[inline]
    fn advance(&mut self, n: usize) {
        self.buf = &self.buf[n.min(self.buf.len())..];
    }
    #[inline]
    fn step_in(&mut self) -> bool {
        match self.limit.checked_sub(1) {
            Some(l) => {
                self.limit = l;
                true
            }
            None => false,
        }
    }
    #[inline]
    fn step_out(&mut self) {
        self.limit += 1;
    }
}

/// What a failed parse adds to the checksum (the same in each language).
const FAIL: u64 = 1 << 40;

/// The setup of a case: a parse that must work. Exit 4 when it does not.
fn need<T>(o: Option<T>) -> T {
    o.unwrap_or_else(|| {
        eprintln!("the parse of the fixture failed");
        std::process::exit(4)
    })
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// The CBOR of the result of one op of a case: the parsed value encoded again, or the output of an encode.
fn check<L: Lib>(case: &str, bytes: &[u8]) -> Vec<u8> {
    match case {
        "bytes_encode" | "bytes_parse" => L::ser(&need(L::de::<Blob>(bytes))),
        "records_encode" | "records_parse" | "unknown_parse" => {
            L::ser(&need(L::de::<Vec<Rec>>(bytes)))
        }
        "dict_parse" => L::ser(&need(L::de::<HashMap<String, u64>>(bytes))),
        "text_parse" => L::ser(&need(L::de::<Vec<String>>(bytes))),
        "records_value" | "map_value" | "deep_parse" | "deep_encode" => {
            L::ser_value(&need(L::de_value(bytes)))
        }
        _ => unreachable!(),
    }
}

fn repeat(loops: u64, mut work: impl FnMut() -> u64) -> u64 {
    let mut sum = 0u64;
    for _ in 0..loops {
        sum = sum.wrapping_add(black_box(work()));
    }
    sum
}

fn run<L: Lib>(case: &str, loops: u64, bytes: Vec<u8>) -> u64 {
    let bytes = black_box(bytes);
    let len_or = |o: Option<usize>| o.map_or(FAIL, |n| n as u64);
    match case {
        "bytes_encode" => {
            let b: Blob = need(L::de(&bytes));
            repeat(loops, || L::ser(&b).len() as u64)
        }
        "bytes_parse" => repeat(loops, || len_or(L::de::<Blob>(&bytes).map(|b| b.len()))),
        "records_encode" => {
            let rs: Vec<Rec> = need(L::de(&bytes));
            repeat(loops, || L::ser(&rs).len() as u64)
        }
        "records_parse" | "unknown_parse" => {
            repeat(loops, || len_or(L::de::<Vec<Rec>>(&bytes).map(|r| r.len())))
        }
        "dict_parse" => repeat(loops, || {
            len_or(L::de::<HashMap<String, u64>>(&bytes).map(|d| d.len()))
        }),
        "text_parse" => repeat(loops, || len_or(L::de::<Vec<String>>(&bytes).map(|t| t.len()))),
        "records_value" | "map_value" | "deep_parse" => {
            repeat(loops, || L::de_value(&bytes).map_or(FAIL, |_| 1))
        }
        "deep_encode" => {
            let v = need(L::de_value(&bytes));
            repeat(loops, || L::ser_value(&v).len() as u64)
        }
        _ => unreachable!(),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 5 {
        eprintln!("usage: cbor-bench <ciborium|cbor4ii> <case> <loops | check> <fixture_dir>");
        std::process::exit(2);
    }
    let (lib, case, dir) = (&args[1], &args[2], &args[4]);
    let bytes = fixture(dir, fixture_of(case));
    if args[3] == "check" {
        let out = match lib.as_str() {
            "ciborium" => check::<Ciborium>(case, &bytes),
            "cbor4ii" => check::<Cbor4ii>(case, &bytes),
            _ => {
                eprintln!("unknown lib: {lib}");
                std::process::exit(2)
            }
        };
        println!("{}", hex(&out));
        return;
    }
    let loops: u64 = args[3].parse().unwrap_or_else(|_| {
        eprintln!("bad loop count: {}", args[3]);
        std::process::exit(2)
    });
    let sum = match lib.as_str() {
        "ciborium" => run::<Ciborium>(case, loops, bytes),
        "cbor4ii" => run::<Cbor4ii>(case, loops, bytes),
        _ => {
            eprintln!("unknown lib: {lib}");
            std::process::exit(2)
        }
    };
    println!("{case} {loops} {sum}");
}
