const mer = @import("mer");

pub const meta: mer.Meta = .{ .title = "404" };

pub fn render(req: mer.Request) mer.Response {
    _ = req;
    return .{ .status = .not_found, .content_type = .html, .body = "<h1>404 — Not Found</h1><p style=\"color:#8a8478;margin-top:12px;\">The page you are looking for does not exist.</p><p style=\"margin-top:20px;\"><a href=\"/\" style=\"color:#3b82f6;\">Back to codedb</a></p>" };
}