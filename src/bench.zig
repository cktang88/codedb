const std = @import("std");
const Explorer = @import("explore.zig").Explorer;
const index = @import("index.zig");
const WordIndex = index.WordIndex;
const TrigramIndex = index.TrigramIndex;
const Trigram = index.Trigram;
const PostingMask = index.PostingMask;
const packTrigram = index.packTrigram;
const normalizeChar = index.normalizeChar;
const FileEntry = struct { name: []const u8, content: []const u8 };

fn generateCode(allocator: std.mem.Allocator, num_files: usize, lines_per_file: usize) ![]const FileEntry {
    var files: std.ArrayList(FileEntry) = .{};
    var prng = std.Random.DefaultPrng.init(42);
    const rand = prng.random();

    const words = [_][]const u8{
        "fn", "pub", "const", "var", "struct", "enum", "union", "return",
        "if", "else", "while", "for", "switch", "break", "continue",
        "try", "catch", "error", "void", "bool", "u8", "u32", "u64",
        "allocator", "self", "result", "value", "index", "count", "size",
        "init", "deinit", "append", "remove", "get", "put", "insert",
        "handleRequest", "processData", "validateInput", "parseConfig",
        "readFile", "writeOutput", "createBuffer", "destroyBuffer",
        "AgentRegistry", "FileVersions", "TrigramIndex", "WordIndex",
        "Explorer", "Store", "Version", "Symbol", "Outline", "Language",
    };

    for (0..num_files) |i| {
        var buf: std.ArrayList(u8) = .{};
        const w = buf.writer(allocator);
        for (0..lines_per_file) |_| {
            const num_words = 5 + rand.intRangeAtMost(usize, 0, 10);
            for (0..num_words) |wi| {
                if (wi > 0) w.writeByte(' ') catch {};
                const word = words[rand.intRangeAtMost(usize, 0, words.len - 1)];
                w.writeAll(word) catch {};
            }
            w.writeByte('\n') catch {};
        }
        var name_buf: [64]u8 = undefined;
        const name = std.fmt.bufPrint(&name_buf, "src/gen_{d}.zig", .{i}) catch unreachable;
        try files.append(allocator, .{
            .name = try allocator.dupe(u8, name),
            .content = try buf.toOwnedSlice(allocator),
        });
    }
    return files.toOwnedSlice(allocator);
}

pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    const num_files = 500;
    const lines_per = 200;
    const total_lines = num_files * lines_per;

    std.debug.print("Generating {d} files × {d} lines = {d} total lines...\n", .{ num_files, lines_per, total_lines });

    const files = try generateCode(allocator, num_files, lines_per);
    defer {
        for (files) |f| {
            allocator.free(f.name);
            allocator.free(f.content);
        }
        allocator.free(files);
    }

    var total_bytes: usize = 0;
    for (files) |f| total_bytes += f.content.len;
    std.debug.print("Total content: {d} KB\n\n", .{total_bytes / 1024});

    // ── Index directly into WordIndex + TrigramIndex ──
    var wi = WordIndex.init(allocator);
    defer wi.deinit();
    var ti = TrigramIndex.init(allocator);
    defer ti.deinit();

    // Also store content for brute force comparison
    var contents = std.StringHashMap([]const u8).init(allocator);
    defer contents.deinit();

    var timer = try std.time.Timer.start();
    for (files) |f| {
        try wi.indexFile(f.name, f.content);
        try ti.indexFile(f.name, f.content);
        try contents.put(f.name, f.content);
    }
    const index_ns = timer.read();
    std.debug.print("Index {d} files:           {d:.1} ms\n", .{ num_files, @as(f64, @floatFromInt(index_ns)) / 1_000_000.0 });

    // ── Bench: raw word index lookup (zero-alloc) ──
    const word_queries = [_][]const u8{ "handleRequest", "AgentRegistry", "allocator", "Explorer", "TrigramIndex" };

    timer.reset();
    const word_iters: usize = 100_000;
    var total_hits: usize = 0;
    for (0..word_iters) |_| {
        for (word_queries) |q| {
            const hits = wi.search(q);
            total_hits += hits.len;
        }
    }
    const word_ns = timer.read();
    const word_total = word_iters * word_queries.len;
    std.debug.print("Word lookup ×{d}:    {d:.1} ms total, {d:.0} ns/query ({d} hits)\n", .{
        word_total,
        @as(f64, @floatFromInt(word_ns)) / 1_000_000.0,
        @as(f64, @floatFromInt(word_ns)) / @as(f64, @floatFromInt(word_total)),
        total_hits / word_iters,
    });

    // ── Bench: trigram candidate lookup (with bloom filtering) ──
    const tri_queries = [_][]const u8{ "handleRequest", "processData", "AgentRegistry", "pub fn init", "TrigramIndex" };

    timer.reset();
    const tri_iters: usize = 10_000;
    for (0..tri_iters) |_| {
        for (tri_queries) |q| {
            const cands = ti.candidates(q);
            if (cands) |c| allocator.free(c);
        }
    }
    const tri_ns = timer.read();
    const tri_total = tri_iters * tri_queries.len;
    std.debug.print("Trigram candidates ×{d}: {d:.1} ms total, {d:.0} ns/query\n", .{
        tri_total,
        @as(f64, @floatFromInt(tri_ns)) / 1_000_000.0,
        @as(f64, @floatFromInt(tri_ns)) / @as(f64, @floatFromInt(tri_total)),
    });

    // ── Bloom filter effectiveness: candidate set sizes ──
    std.debug.print("\n── Bloom Filter Effectiveness ──\n", .{});
    for (tri_queries) |q| {
        // Get candidate count with bloom filtering (current behavior)
        const bloom_cands = ti.candidates(q);
        const bloom_count = if (bloom_cands) |c| blk: {
            defer allocator.free(c);
            break :blk c.len;
        } else num_files;

        // Count candidates from pure trigram intersection (no bloom)
        // by counting files present in ALL trigram posting lists
        var pure_count: usize = 0;
        if (q.len >= 3) {
            const tri_count = q.len - 2;
            var unique = std.AutoHashMap(Trigram, void).init(allocator);
            defer unique.deinit();
            for (0..tri_count) |j| {
                const tri = packTrigram(
                    normalizeChar(q[j]),
                    normalizeChar(q[j + 1]),
                    normalizeChar(q[j + 2]),
                );
                unique.put(tri, {}) catch {};
            }

            // Collect posting list pointers
            var sets: std.ArrayList(*const std.StringHashMap(PostingMask)) = .{};
            defer sets.deinit(allocator);
            var all_found = true;
            var tri_iter = unique.keyIterator();
            while (tri_iter.next()) |tri_ptr| {
                if (ti.index.getPtr(tri_ptr.*)) |file_set| {
                    sets.append(allocator, file_set) catch {};
                } else {
                    all_found = false;
                    break;
                }
            }

            if (all_found and sets.items.len > 0) {
                // Find smallest set, intersect
                var min_idx: usize = 0;
                var min_count: usize = sets.items[0].count();
                for (sets.items[1..], 1..) |set, idx| {
                    if (set.count() < min_count) {
                        min_count = set.count();
                        min_idx = idx;
                    }
                }
                var it = sets.items[min_idx].keyIterator();
                while (it.next()) |path_ptr| {
                    var ok = true;
                    for (sets.items, 0..) |set, idx| {
                        if (idx == min_idx) continue;
                        if (!set.contains(path_ptr.*)) {
                            ok = false;
                            break;
                        }
                    }
                    if (ok) pure_count += 1;
                }
            }
        }

        // Count actual matches via brute force
        var actual_count: usize = 0;
        var c_iter = contents.iterator();
        while (c_iter.next()) |entry| {
            if (std.mem.indexOf(u8, entry.value_ptr.*, q) != null) {
                actual_count += 1;
            }
        }

        const reduction = if (pure_count > 0) @as(f64, @floatFromInt(pure_count - bloom_count)) / @as(f64, @floatFromInt(pure_count)) * 100.0 else 0.0;
        std.debug.print("  \"{s}\":\n    trigram-only={d}  bloom={d}  actual={d}  reduction={d:.0}%\n", .{
            q, pure_count, bloom_count, actual_count, reduction,
        });
    }

    // ── Bench: brute force substring search ──
    timer.reset();
    const brute_iters: usize = 1_000;
    for (0..brute_iters) |_| {
        for (tri_queries) |q| {
            var iter = contents.iterator();
            while (iter.next()) |entry| {
                _ = std.mem.indexOf(u8, entry.value_ptr.*, q);
            }
        }
    }
    const brute_ns = timer.read();
    const brute_total = brute_iters * tri_queries.len;
    std.debug.print("\nBrute force ×{d}:      {d:.1} ms total, {d:.0} ns/query\n", .{
        brute_total,
        @as(f64, @floatFromInt(brute_ns)) / 1_000_000.0,
        @as(f64, @floatFromInt(brute_ns)) / @as(f64, @floatFromInt(brute_total)),
    });

    std.debug.print("\n── Summary ({d} files, {d}K lines, {d} KB) ──\n", .{ num_files, total_lines / 1000, total_bytes / 1024 });
    std.debug.print("Word index:    {d:.0} ns/query  (zero-alloc hash lookup)\n", .{@as(f64, @floatFromInt(word_ns)) / @as(f64, @floatFromInt(word_total))});
    std.debug.print("Trigram:       {d:.0} ns/query  (candidate set + bloom filter)\n", .{@as(f64, @floatFromInt(tri_ns)) / @as(f64, @floatFromInt(tri_total))});
    std.debug.print("Brute force:   {d:.0} ns/query  (linear scan all content)\n", .{@as(f64, @floatFromInt(brute_ns)) / @as(f64, @floatFromInt(brute_total))});
    const speedup_word = @as(f64, @floatFromInt(brute_ns)) / @as(f64, @floatFromInt(brute_total)) / (@as(f64, @floatFromInt(word_ns)) / @as(f64, @floatFromInt(word_total)));
    const speedup_tri = @as(f64, @floatFromInt(brute_ns)) / @as(f64, @floatFromInt(brute_total)) / (@as(f64, @floatFromInt(tri_ns)) / @as(f64, @floatFromInt(tri_total)));
    std.debug.print("Word vs brute: {d:.0}× faster\n", .{speedup_word});
    std.debug.print("Tri vs brute:  {d:.1}× faster\n", .{speedup_tri});
}
