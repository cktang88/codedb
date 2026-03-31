const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const merjs_dep = b.dependency("merjs", .{
        .target = target,
        .optimize = optimize,
    });
    const mer_mod = merjs_dep.module("mer");

    // Use LOCAL src/main.zig (with our generated/routes.zig)
    const main_mod = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
    });
    main_mod.addImport("mer", mer_mod);

    // Register app/ pages
    addAppPages(b, main_mod, mer_mod, "app", "app");

    const exe = b.addExecutable(.{
        .name = "codedb-site",
        .root_module = main_mod,
    });
    b.installArtifact(exe);

    // ── zig build serve ──────────────────────────
    const run_cmd = b.addRunArtifact(exe);
    if (b.args) |args| run_cmd.addArgs(args);

    const serve_step = b.step("serve", "Start dev server");
    serve_step.dependOn(&run_cmd.step);
}

fn addAppPages(
    b: *std.Build,
    main_mod: *std.Build.Module,
    mer_mod: *std.Build.Module,
    comptime dir: []const u8,
    comptime prefix: []const u8,
) void {
    const app_dir = b.build_root.handle.openDir(dir, .{ .iterate = true }) catch return;
    var it = app_dir.iterate();
    while (it.next() catch null) |entry| {
        if (entry.kind != .file) continue;
        const name = entry.name;
        if (!std.mem.endsWith(u8, name, ".zig")) continue;

        const stem = name[0 .. name.len - 4];
        const import_name = b.fmt("{s}/{s}", .{ prefix, stem });
        const page_mod = b.createModule(.{
            .root_source_file = b.path(b.fmt("{s}/{s}", .{ dir, name })),
        });
        page_mod.addImport("mer", mer_mod);
        main_mod.addImport(import_name, page_mod);
    }
}
