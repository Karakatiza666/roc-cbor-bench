//! The Zig side of roc-cbor-bench. Usage: cbor-bench <case> <loops | check> <fixture_dir>
//! Library: zbor (comptime-derived parse and stringify of Zig types). The contract of each case is in
//! README.md. Each loop parses into an arena on the C allocator, freed after the loop
//! (the usual Zig pattern: zbor has no free function for a parsed value).
//! Not supported by zbor, exit 3 (the runner records n/a): a dynamic value tree (records_value,
//! map_value, deep_*: zbor's DataItem is a checked view, not a tree), and nesting deeper than 128.
//! dict_parse uses zbor's map iterator into a std.StringHashMap (parse has no hash map target).
//! zbor's parse copies each byte string and text into the allocator (it does not borrow from the input),
//! the same as the other libraries here except roc-cbor's `Cbor.Bytes`.
//! A failed parse adds FAIL to the checksum. `check` prints the CBOR of the result of one op as hex (the
//! parsed value encoded again, or the output of an encode); run.py compares it with the fixture.
const std = @import("std");
const zbor = @import("zbor");

const Rec = struct {
    id: u64,
    name: []const u8,
    ok: bool,
    score: f64,
    tags: []const []const u8,
};

fn fixtureOf(case: []const u8) ?[]const u8 {
    const pairs = .{
        .{ "bytes_encode", "bytes" },   .{ "bytes_parse", "bytes" },
        .{ "records_encode", "records" }, .{ "records_parse", "records" },
        .{ "records_value", "records" }, .{ "unknown_parse", "wide" },
        .{ "dict_parse", "dict" },       .{ "map_value", "dict" },
        .{ "text_parse", "texts" },      .{ "deep_encode", "deep" },
        .{ "deep_parse", "deep" },
    };
    inline for (pairs) |p| if (std.mem.eql(u8, case, p[0])) return p[1];
    return null;
}

var sink: u64 = 0;

/// What a failed parse adds to the checksum (the same in each language).
const FAIL: u64 = 1 << 40;

/// The setup of a case: a parse that must work. Exit 4 when it does not.
fn need(comptime T: type, data: []const u8, a: std.mem.Allocator) T {
    const di = zbor.DataItem.new(data) catch die();
    return zbor.parse(T, di, .{ .allocator = a }) catch die();
}

fn die() noreturn {
    std.debug.print("the parse of the fixture failed\n", .{});
    std.process.exit(4);
}

fn encodeLen(gpa: std.mem.Allocator, value: anytype, options: zbor.Options) !u64 {
    var w = std.Io.Writer.Allocating.init(gpa);
    defer w.deinit();
    try zbor.stringify(value, options, &w.writer);
    return w.written().len;
}

fn parseLen(comptime T: type, data: []const u8, gpa: std.mem.Allocator) u64 {
    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const di = zbor.DataItem.new(data) catch return FAIL;
    const v = zbor.parse(T, di, .{ .allocator = arena.allocator() }) catch return FAIL;
    return v.len;
}

fn dictParse(data: []const u8, a: std.mem.Allocator) ?std.StringHashMap(u64) {
    const di = zbor.DataItem.new(data) catch return null;
    var it = di.map() orelse return null;
    var map = std.StringHashMap(u64).init(a);
    map.ensureTotalCapacity(@intCast(it.len)) catch return null;
    while (it.next()) |pair| {
        const k = zbor.parse([]const u8, pair.key, .{ .allocator = a }) catch return null;
        const v = zbor.parse(u64, pair.value, .{}) catch return null;
        const gop = map.getOrPut(k) catch return null;
        if (gop.found_existing) return null; // a duplicate key
        gop.value_ptr.* = v;
    }
    return map;
}

fn dictLen(data: []const u8, gpa: std.mem.Allocator) u64 {
    var arena = std.heap.ArenaAllocator.init(gpa);
    defer arena.deinit();
    const map = dictParse(data, arena.allocator()) orelse return FAIL;
    return map.count();
}

/// The CBOR of a map of text to integer (zbor's stringify has no hash map source).
fn writeDict(map: std.StringHashMap(u64), w: *std.Io.Writer) !void {
    const n: u64 = map.count();
    if (n < 24) {
        try w.writeByte(0xa0 | @as(u8, @intCast(n)));
    } else if (n <= 0xffff_ffff) {
        try w.writeByte(0xba);
        try w.writeInt(u32, @intCast(n), .big);
    } else {
        try w.writeByte(0xbb);
        try w.writeInt(u64, n, .big);
    }
    var it = map.iterator();
    while (it.next()) |e| {
        try zbor.builder.writeTextString(w, e.key_ptr.*);
        try zbor.builder.writeInt(w, e.value_ptr.*);
    }
}

/// The CBOR of the result of one op of a case: the parsed value encoded again, or the output of an encode.
fn check(case: []const u8, data: []const u8, gpa: std.mem.Allocator, w: *std.Io.Writer) !void {
    const text: zbor.Options = .{ .slice_serialization_type = .TextString };
    if (std.mem.eql(u8, case, "bytes_encode") or std.mem.eql(u8, case, "bytes_parse")) {
        try zbor.stringify(need([]const u8, data, gpa), .{}, w);
    } else if (std.mem.eql(u8, case, "records_encode") or std.mem.eql(u8, case, "records_parse") or
        std.mem.eql(u8, case, "unknown_parse"))
    {
        try zbor.stringify(need([]const Rec, data, gpa), text, w);
    } else if (std.mem.eql(u8, case, "text_parse")) {
        try zbor.stringify(need([]const []const u8, data, gpa), text, w);
    } else if (std.mem.eql(u8, case, "dict_parse")) {
        try writeDict(dictParse(data, gpa) orelse die(), w);
    } else {
        std.debug.print("unsupported: {s}\n", .{case});
        std.process.exit(3);
    }
}

pub fn main(init: std.process.Init) !void {
    const gpa = std.heap.c_allocator;
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    if (args.len != 4) {
        std.debug.print("usage: cbor-bench <case> <loops> <fixture_dir>\n", .{});
        std.process.exit(2);
    }
    const case = args[1];
    const fx = fixtureOf(case) orelse {
        std.debug.print("unknown case: {s}\n", .{case});
        std.process.exit(2);
    };
    const path = try std.fmt.allocPrint(gpa, "{s}/{s}.cbor", .{ args[3], fx });
    const data = try std.Io.Dir.cwd().readFileAlloc(init.io, path, gpa, .unlimited);
    if (std.mem.eql(u8, args[2], "check")) {
        var cbor = std.Io.Writer.Allocating.init(gpa);
        try check(case, data, gpa, &cbor.writer);
        var hbuf: [4096]u8 = undefined;
        var hout = std.Io.File.stdout().writer(init.io, &hbuf);
        try hout.interface.print("{x}\n", .{cbor.written()});
        try hout.interface.flush();
        return;
    }
    const loops = std.fmt.parseInt(u64, args[2], 10) catch {
        std.debug.print("bad loop count: {s}\n", .{args[2]});
        std.process.exit(2);
    };

    var sum: u64 = 0;
    if (std.mem.eql(u8, case, "bytes_encode")) {
        const blob = need([]const u8, data, gpa);
        for (0..loops) |_| sum +%= try encodeLen(gpa, blob, .{});
    } else if (std.mem.eql(u8, case, "bytes_parse")) {
        for (0..loops) |_| sum +%= parseLen([]const u8, data, gpa);
    } else if (std.mem.eql(u8, case, "records_encode")) {
        const rs = need([]const Rec, data, gpa);
        for (0..loops) |_| sum +%= try encodeLen(gpa, rs, .{ .slice_serialization_type = .TextString });
    } else if (std.mem.eql(u8, case, "records_parse") or std.mem.eql(u8, case, "unknown_parse")) {
        for (0..loops) |_| sum +%= parseLen([]const Rec, data, gpa);
    } else if (std.mem.eql(u8, case, "text_parse")) {
        for (0..loops) |_| sum +%= parseLen([]const []const u8, data, gpa);
    } else if (std.mem.eql(u8, case, "dict_parse")) {
        for (0..loops) |_| sum +%= dictLen(data, gpa);
    } else {
        std.debug.print("unsupported: {s}\n", .{case});
        std.process.exit(3);
    }
    @as(*volatile u64, &sink).* = sum;

    var buf: [256]u8 = undefined;
    var out = std.Io.File.stdout().writer(init.io, &buf);
    try out.interface.print("{s} {d} {d}\n", .{ case, loops, sum });
    try out.interface.flush();
}
