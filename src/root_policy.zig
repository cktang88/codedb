const std = @import("std");

fn isExactOrChild(path: []const u8, prefix: []const u8) bool {
    if (!std.mem.startsWith(u8, path, prefix)) return false;
    return path.len == prefix.len or path[prefix.len] == '/';
}

pub fn isIndexableRoot(path: []const u8) bool {
    if (isExactOrChild(path, "/private/tmp")) return false;
    if (isExactOrChild(path, "/tmp")) return false;
    if (isExactOrChild(path, "/var/tmp")) return false;
    return true;
}
