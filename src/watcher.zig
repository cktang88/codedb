const std = @import("std");
const Store = @import("store.zig").Store;
const Explorer = @import("explore.zig").Explorer;

pub const EventKind = enum(u8) {
    created,
    modified,
    deleted,
};

pub const FsEvent = struct {
    path_buf: [std.fs.max_path_bytes]u8 = undefined,
    path_len: usize,
    kind: EventKind,
    seq: u64,

    pub fn init(src_path: []const u8, kind: EventKind, seq: u64) FsEvent {
        std.debug.assert(src_path.len <= std.fs.max_path_bytes);
        var event = FsEvent{
            .path_len = src_path.len,
            .kind = kind,
            .seq = seq,
        };
        @memcpy(event.path_buf[0..src_path.len], src_path);
        return event;
    }

    pub fn path(self: *const FsEvent) []const u8 {
        return self.path_buf[0..self.path_len];
    }
};

pub const EventQueue = struct {
    const CAPACITY = 4096;

    events: [CAPACITY]?FsEvent = [_]?FsEvent{null} ** CAPACITY,
    head: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
    tail: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),

    pub fn push(self: *EventQueue, event: FsEvent) bool {
        const cur_tail = self.tail.load(.monotonic);
        const next_tail = (cur_tail + 1) % CAPACITY;
        if (next_tail == self.head.load(.acquire)) return false;
        self.events[cur_tail] = event;
        self.tail.store(next_tail, .release);
        return true;
    }

    pub fn pop(self: *EventQueue) ?FsEvent {
        const cur_head = self.head.load(.monotonic);
        if (cur_head == self.tail.load(.acquire)) return null;
        const event = self.events[cur_head];
        self.head.store((cur_head + 1) % CAPACITY, .release);
        return event;
    }
};

const FileState = struct {
    mtime: i64,   // seconds — cheap stat check
    hash: u64,    // wyhash of content — confirms actual change
};

const FileMap = std.StringHashMap(FileState);

const skip_dirs = [_][]const u8{
    ".git",
    ".codedb",
    "node_modules",
    ".zig-cache",
    "zig-out",
    ".next",
    ".nuxt",
    ".svelte-kit",
    "dist",
    "build",
    ".build",
    ".output",
    "out",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",          // rust, java/maven
    ".gradle",
    ".idea",
    ".vs",
    "vendor",          // go, php
    "Pods",            // cocoapods
    ".dart_tool",
    ".pub-cache",
    "coverage",
    ".nyc_output",
    ".turbo",
    ".parcel-cache",
    ".cache",
    ".tmp",
    ".temp",
    ".DS_Store",
};

fn shouldSkip(path: []const u8) bool {
    // Check each path component against skip list
    var rest = path;
    while (true) {
        for (skip_dirs) |skip| {
            if (rest.len >= skip.len and
                std.mem.eql(u8, rest[0..skip.len], skip) and
                (rest.len == skip.len or rest[skip.len] == '/'))
                return true;
        }
        // Advance to next component
        if (std.mem.indexOfScalar(u8, rest, '/')) |sep| {
            rest = rest[sep + 1 ..];
        } else break;
    }
    return false;
}

/// Called from main thread to do the initial scan before listening.
pub fn initialScan(store: *Store, explorer: *Explorer, root: []const u8, allocator: std.mem.Allocator) !void {
    var dir = try std.fs.cwd().openDir(root, .{ .iterate = true });
    defer dir.close();

    var walker = try dir.walk(allocator);
    defer walker.deinit();

    while (try walker.next()) |entry| {
        if (entry.kind != .file) continue;
        if (shouldSkip(entry.path)) continue;

        const stat = dir.statFile(entry.path) catch continue;
        _ = try store.recordSnapshot(entry.path, stat.size, 0);
        // Index outline + content (skip word/trigram for speed)
        indexFileOutline(explorer, dir, entry.path, allocator) catch {};
    }
}

/// Fast index: parse symbols/outline only, skip expensive word+trigram indexes.
fn indexFileOutline(explorer: *Explorer, dir: std.fs.Dir, path: []const u8, allocator: std.mem.Allocator) !void {
    if (shouldSkipFile(path)) return;
    const file = try dir.openFile(path, .{});
    defer file.close();
    const stat = try file.stat();
    if (stat.size > 512 * 1024) return;
    const content = try file.readToEndAlloc(allocator, 512 * 1024);
    defer allocator.free(content);
    const check_len = @min(content.len, 512);
    for (content[0..check_len]) |c| {
        if (c == 0) return;
    }
    try explorer.indexFileOutlineOnly(path, content);
}

/// Background thread: polls for incremental FS changes.
pub fn incrementalLoop(store: *Store, explorer: *Explorer, queue: *EventQueue, root: []const u8) void {
    const backing = std.heap.page_allocator;

    var known = FileMap.init(backing);
    defer known.deinit();

    // Build initial snapshot: stat every file, hash content for indexable ones
    {
        var snap_arena = std.heap.ArenaAllocator.init(backing);
        defer snap_arena.deinit();
        const tmp = snap_arena.allocator();
        var dir = std.fs.cwd().openDir(root, .{ .iterate = true }) catch return;
        defer dir.close();
        var walker = dir.walk(tmp) catch return;
        defer walker.deinit();
        while (walker.next() catch null) |entry| {
            if (entry.kind != .file) continue;
            if (shouldSkip(entry.path)) continue;
            const stat = dir.statFile(entry.path) catch continue;
            const mtime: i64 = @intCast(@divTrunc(stat.mtime, std.time.ns_per_ms));
            const hash = hashFile(dir, entry.path, tmp) catch 0;
            const duped = backing.dupe(u8, entry.path) catch continue;
            known.put(duped, .{ .mtime = mtime, .hash = hash }) catch backing.free(duped);
        }
    }

    while (true) {
        // Poll every 2s — gentle on CPU, fast enough to catch saves
        std.Thread.sleep(2 * std.time.ns_per_s);

        // Each diff cycle gets its own arena so temporaries are freed
        var cycle_arena = std.heap.ArenaAllocator.init(backing);
        defer cycle_arena.deinit();

        incrementalDiff(store, explorer, queue, &known, root, backing, cycle_arena.allocator()) catch |err| {
            std.log.err("watcher: diff failed: {}", .{err});
        };
    }
}

fn hashFile(dir: std.fs.Dir, path: []const u8, allocator: std.mem.Allocator) !u64 {
    if (shouldSkipFile(path)) return 0;
    const file = dir.openFile(path, .{}) catch return 0;
    defer file.close();
    const stat = file.stat() catch return 0;
    if (stat.size > 512 * 1024) return 0;
    const content = file.readToEndAlloc(allocator, 512 * 1024) catch return 0;
    defer allocator.free(content);
    return std.hash.Wyhash.hash(0, content);
}

fn pushEventOrWait(queue: *EventQueue, event: FsEvent) void {
    // Preserve prior drop-on-full behavior so producer never stalls permanently.
    _ = queue.push(event);
}


fn incrementalDiff(store: *Store, explorer: *Explorer, queue: *EventQueue, known: *FileMap, root: []const u8, persistent: std.mem.Allocator, tmp: std.mem.Allocator) !void {
    var dir = try std.fs.cwd().openDir(root, .{ .iterate = true });
    defer dir.close();

    var seen = std.StringHashMap(void).init(tmp);

    var walker = try dir.walk(tmp);
    defer walker.deinit();

    while (try walker.next()) |entry| {
        if (entry.kind != .file) continue;
        if (shouldSkip(entry.path)) continue;

        const seen_key = try tmp.dupe(u8, entry.path);
        const gop = try seen.getOrPut(seen_key);
        if (gop.found_existing) continue;

        const stat = dir.statFile(entry.path) catch continue;
        const mtime: i64 = @intCast(@divTrunc(stat.mtime, std.time.ns_per_ms));

        if (known.get(entry.path)) |old| {
            // Mtime unchanged -> skip (cheap path, no IO)
            if (old.mtime == mtime) continue;

            // Mtime changed -> hash to confirm content actually differs
            const hash = hashFile(dir, entry.path, tmp) catch 0;
            if (hash != 0 and hash == old.hash) {
                // Content identical (e.g. touch, git checkout) -> update mtime only
                try known.put(entry.path, .{ .mtime = mtime, .hash = old.hash });
                continue;
            }

            const seq = try store.recordSnapshot(entry.path, stat.size, hash);
            try known.put(entry.path, .{ .mtime = mtime, .hash = hash });
            const stable_path = known.getKey(entry.path).?;
            pushEventOrWait(queue, FsEvent.init(stable_path, .modified, seq));
            indexFileContent(explorer, dir, entry.path, tmp) catch {};
        } else {
            // New file
            const hash = hashFile(dir, entry.path, tmp) catch 0;
            const duped = try persistent.dupe(u8, entry.path);
            errdefer persistent.free(duped);
            const seq = try store.recordSnapshot(duped, stat.size, hash);
            try known.put(duped, .{ .mtime = mtime, .hash = hash });
            pushEventOrWait(queue, FsEvent.init(duped, .created, seq));
            indexFileContent(explorer, dir, duped, tmp) catch {};
        }
    }

    // Detect deleted files
    var to_remove: std.ArrayList([]const u8) = .{};
    defer to_remove.deinit(tmp);

    var iter = known.iterator();
    while (iter.next()) |kv| {
        if (!seen.contains(kv.key_ptr.*)) {
            try to_remove.append(tmp, kv.key_ptr.*);
        }
    }
    for (to_remove.items) |path| {
        const seq = store.recordDelete(path, 0) catch continue;
        explorer.removeFile(path);
        if (known.fetchRemove(path)) |kv| {
            pushEventOrWait(queue, FsEvent.init(kv.key, .deleted, seq));
            persistent.free(kv.key);
        }
    }
}

const skip_extensions = [_][]const u8{
    ".png",  ".jpg",  ".jpeg", ".gif",  ".bmp",  ".ico",  ".icns", ".webp",
    ".svg",  ".ttf",  ".otf",  ".woff", ".woff2", ".eot",
    ".zip",  ".tar",  ".gz",   ".bz2",  ".xz",   ".7z",  ".rar",
    ".pdf",  ".doc",  ".docx", ".xls",  ".xlsx", ".pptx",
    ".mp3",  ".mp4",  ".wav",  ".avi",  ".mov",  ".flv",  ".ogg",  ".webm",
    ".exe",  ".dll",  ".so",   ".dylib", ".o",   ".a",    ".lib",
    ".wasm", ".pyc",  ".pyo",  ".class",
    ".db",   ".sqlite", ".sqlite3",
    ".lock", ".sum",
};

fn shouldSkipFile(path: []const u8) bool {
    for (skip_extensions) |ext| {
        if (std.mem.endsWith(u8, path, ext)) return true;
    }
    // Skip dotfiles like .DS_Store, .gitignore etc at any depth
    if (std.mem.endsWith(u8, path, ".DS_Store")) return true;
    return false;
}

fn indexFileContent(explorer: *Explorer, dir: std.fs.Dir, path: []const u8, allocator: std.mem.Allocator) !void {
    if (shouldSkipFile(path)) return;
    const file = try dir.openFile(path, .{});
    defer file.close();
    const stat = try file.stat();
    // Skip files over 512KB (likely minified bundles or generated)
    if (stat.size > 512 * 1024) return;
    const content = try file.readToEndAlloc(allocator, 512 * 1024);
    defer allocator.free(content);
    // Skip binary content (check first 512 bytes for null bytes)
    const check_len = @min(content.len, 512);
    for (content[0..check_len]) |c| {
        if (c == 0) return;
    }
    try explorer.indexFile(path, content);
}
