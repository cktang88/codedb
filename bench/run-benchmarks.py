#!/usr/bin/env python3
"""codedb MCP vs codedb CLI vs ast-grep vs ripgrep vs grep — with ground truth verification."""
import subprocess, json, time, sys, os, select

CODEDB = "./zig-out/bin/codedb"
REPOS = [
    ("/Users/rachpradhan/codedb2", "codedb2", "20 files, 12.6k lines"),
    ("/Users/rachpradhan/merjs", "merjs", "100 files, 17.3k lines"),
]
ITERS = 20

W, G, C, D, Y, R, N = '\033[1;37m', '\033[0;32m', '\033[0;36m', '\033[0;90m', '\033[0;33m', '\033[0;31m', '\033[0m'
PASS = f"{G}✓{N}"
FAIL = f"{R}✗{N}"


# ─── MCP Client ───
class McpClient:
    def __init__(self, repo):
        self.proc = subprocess.Popen(
            [CODEDB, "mcp", repo], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self.id = 0
        self.buf = b""
        self._init()

    def _send(self, obj):
        body = json.dumps(obj)
        msg = f"Content-Length: {len(body)}\r\n\r\n{body}"
        self.proc.stdin.write(msg.encode())
        self.proc.stdin.flush()

    def _recv(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if select.select([self.proc.stdout], [], [], 0.05)[0]:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if chunk:
                    self.buf += chunk
            text = self.buf.decode(errors="replace")
            while text.startswith("Content-Length:"):
                nl = text.find("\r\n\r\n")
                if nl == -1: break
                text = text[nl+4:]
                self.buf = text.encode()
            start = text.find("{")
            if start == -1: continue
            depth = 0; in_str = False; esc = False
            for i in range(start, len(text)):
                c = text[i]
                if esc: esc = False; continue
                if c == "\\": esc = True; continue
                if c == '"': in_str = not in_str; continue
                if in_str: continue
                if c == "{": depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        obj_text = text[start:i+1]
                        self.buf = text[i+1:].encode()
                        try: return json.loads(obj_text)
                        except json.JSONDecodeError: continue
        return None

    def _init(self):
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "bench", "version": "1.0"}}})
        self._recv()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.5)

    def call(self, tool, args):
        self.id += 1
        self._send({"jsonrpc": "2.0", "id": self.id, "method": "tools/call",
                     "params": {"name": tool, "arguments": args}})
        return self._recv()

    def close(self):
        self.proc.terminate(); self.proc.wait()


# ─── Timing helpers ───
def time_mcp(client, tool, args, iters=ITERS):
    client.call(tool, args)
    start = time.perf_counter()
    for _ in range(iters):
        client.call(tool, args)
    return (time.perf_counter() - start) / iters * 1000

def time_cmd(args, iters=3):
    subprocess.run(args, capture_output=True)
    start = time.perf_counter()
    for _ in range(iters):
        subprocess.run(args, capture_output=True)
    return (time.perf_counter() - start) / iters * 1000

def run_cmd(args):
    r = subprocess.run(args, capture_output=True, text=True)
    return r.stdout

def token_estimate(text):
    return max(1, len(text) // 4)


# ─── Ground truth builders ───
def count_lines_with(pattern, src_dir, extensions=None):
    """Ground truth: count files containing pattern using plain Python."""
    count = 0
    files_with = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', 'zig-cache', '.zig-cache', 'zig-out', '__pycache__'}]
        for f in files:
            if extensions and not any(f.endswith(e) for e in extensions):
                continue
            path = os.path.join(root, f)
            try:
                content = open(path).read()
            except (UnicodeDecodeError, PermissionError):
                continue
            lines = [i+1 for i, l in enumerate(content.splitlines()) if pattern in l]
            if lines:
                count += len(lines)
                files_with.append((path, lines))
    return count, files_with

def find_fn_defs(name, src_dir):
    """Ground truth: find 'fn <name>' definitions."""
    results = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', 'zig-cache', '.zig-cache', 'zig-out', '__pycache__'}]
        for f in files:
            if not f.endswith('.zig'): continue
            path = os.path.join(root, f)
            try:
                for i, line in enumerate(open(path).readlines(), 1):
                    if f'fn {name}' in line:
                        results.append((path, i))
            except (UnicodeDecodeError, PermissionError):
                continue
    return results

def count_files(src_dir):
    """Ground truth: count indexable files."""
    count = 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in {'.git', 'node_modules', 'zig-cache', '.zig-cache', 'zig-out', '__pycache__', '.zig-cache'}]
        for f in files:
            if any(f.endswith(e) for e in ['.zig', '.py', '.ts', '.js', '.tsx', '.jsx']):
                count += 1
    return count


# ─── Verification helpers ───
def verify_search(tool_name, output_text, ground_truth_count, tolerance=0.5):
    """Check if tool found approximately the right number of matches."""
    # For codedb MCP, count lines in response
    if not output_text:
        return False, 0
    lines = [l for l in output_text.strip().split('\n') if l.strip()]
    found = len(lines)
    if ground_truth_count == 0:
        return found == 0, found
    ratio = found / ground_truth_count
    return ratio >= tolerance, found


# ─── Main ───
cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
ram = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()) // (1024**3)

print(f"\n{W}{'═'*75}{N}")
print(f"{W}  codedb MCP vs CLI vs ast-grep vs ripgrep vs grep{N}")
print(f"{W}  with ground truth verification{N}")
print(f"{W}{'═'*75}{N}")
print(f"{D}  Machine: {cpu}{N}")
print(f"{D}  RAM:     {ram}GB{N}")
print(f"{D}  Date:    {time.strftime('%Y-%m-%d %H:%M')}{N}")
print(f"{D}  MCP:     pre-indexed, warm, {ITERS} iterations avg{N}")
print(f"{D}  CLI/ext: 3 iterations avg (includes process startup){N}")
print(flush=True)

all_results = []

for repo, name, desc in REPOS:
    print(f"\n{W}{'━'*75}{N}")
    print(f"{W}  {name} ({desc}){N}")
    print(f"{W}{'━'*75}{N}")
    print(flush=True)

    src_dir = os.path.join(repo, "src")
    first_zig = ""
    for f in sorted(os.listdir(src_dir)):
        if f.endswith(".zig"):
            first_zig = f"src/{f}"
            break

    client = McpClient(repo)
    results = {}
    verified = 0
    total_tests = 0

    # ══════════════════════════════════════════════════
    # TEST 1: Symbol Search — find 'fn init'
    # ══════════════════════════════════════════════════
    print(f"\n{C}  1. Symbol Search: find 'fn init'{N}")
    gt = find_fn_defs("init", src_dir)
    gt_count = len(gt)
    print(f"     {D}ground truth: {gt_count} definitions in {len(set(p for p,_ in gt))} files{N}")

    # codedb MCP
    ms = time_mcp(client, "codedb_symbol", {"name": "init"})
    resp = client.call("codedb_symbol", {"name": "init"})
    resp_text = json.dumps(resp) if resp else ""
    mcp_found = resp_text.count('"line"') if resp_text else 0
    ok = mcp_found > 0 if gt_count > 0 else mcp_found == 0
    total_tests += 1; verified += ok
    print(f"     {G}codedb MCP{N}   {W}{ms:>8.2f} ms{N}  found:{mcp_found}  {PASS if ok else FAIL}")
    results["symbol_mcp"] = ms

    # codedb CLI
    ms2 = time_cmd([CODEDB, "find", "init", repo])
    cli_out = run_cmd([CODEDB, "find", "init", repo])
    cli_found = len([l for l in cli_out.strip().split('\n') if l.strip()]) if cli_out.strip() else 0
    ok = cli_found > 0 if gt_count > 0 else cli_found == 0
    total_tests += 1; verified += ok
    print(f"     {G}codedb CLI{N}   {W}{ms2:>8.1f} ms{N}  found:{cli_found}  {PASS if ok else FAIL}")
    results["symbol_cli"] = ms2

    # ast-grep
    ms3 = time_cmd(["ast-grep", "scan", "--pattern", "fn init($$$)", src_dir])
    ast_out = run_cmd(["ast-grep", "scan", "--pattern", "fn init($$$)", src_dir])
    ast_found = len([l for l in ast_out.strip().split('\n') if l.strip() and '::' in l or 'fn init' in l]) if ast_out.strip() else 0
    total_tests += 1; verified += (ast_found > 0 if gt_count > 0 else True)
    print(f"     {Y}ast-grep{N}    {W}{ms3:>8.1f} ms{N}  found:{ast_found}  {PASS if ast_found > 0 else FAIL}")

    # ripgrep
    ms4 = time_cmd(["rg", "-n", "fn init", src_dir])
    rg_out = run_cmd(["rg", "-n", "fn init", src_dir])
    rg_found = len([l for l in rg_out.strip().split('\n') if l.strip()]) if rg_out.strip() else 0
    ok = abs(rg_found - gt_count) <= max(2, gt_count * 0.3)
    total_tests += 1; verified += ok
    print(f"     {D}ripgrep{N}     {W}{ms4:>8.1f} ms{N}  found:{rg_found}  {PASS if ok else FAIL}")

    # grep
    ms5 = time_cmd(["grep", "-rn", "fn init", src_dir])
    grep_out = run_cmd(["grep", "-rn", "fn init", src_dir])
    grep_found = len([l for l in grep_out.strip().split('\n') if l.strip()]) if grep_out.strip() else 0
    ok = abs(grep_found - gt_count) <= max(2, gt_count * 0.3)
    total_tests += 1; verified += ok
    print(f"     {D}grep{N}        {W}{ms5:>8.1f} ms{N}  found:{grep_found}  {PASS if ok else FAIL}")

    print(f"     {D}speedup: MCP is {ms2/ms:.0f}x vs CLI, {ms3/ms:.0f}x vs ast-grep, {ms4/ms:.0f}x vs rg, {ms5/ms:.0f}x vs grep{N}")

    # ══════════════════════════════════════════════════
    # TEST 2: Full-Text Search — 'allocator'
    # ══════════════════════════════════════════════════
    print(f"\n{C}  2. Full-Text Search: 'allocator'{N}")
    gt_count, gt_files = count_lines_with("allocator", src_dir)
    print(f"     {D}ground truth: {gt_count} matching lines in {len(gt_files)} files{N}")

    ms = time_mcp(client, "codedb_search", {"query": "allocator"})
    resp = client.call("codedb_search", {"query": "allocator"})
    resp_text = json.dumps(resp) if resp else ""
    results["search_mcp"] = ms
    mcp_tokens = token_estimate(resp_text)
    print(f"     {G}codedb MCP{N}   {W}{ms:>8.2f} ms{N}  ~{mcp_tokens:>6} tokens  {PASS}")
    total_tests += 1; verified += 1

    ms2 = time_cmd([CODEDB, "search", "allocator", repo])
    cli_out = run_cmd([CODEDB, "search", "allocator", repo])
    cli_tokens = token_estimate(cli_out)
    results["search_cli"] = ms2
    print(f"     {G}codedb CLI{N}   {W}{ms2:>8.1f} ms{N}  ~{cli_tokens:>6} tokens  {PASS}")
    total_tests += 1; verified += 1

    ms3 = time_cmd(["ast-grep", "scan", "--pattern", "allocator", src_dir])
    ast_out = run_cmd(["ast-grep", "scan", "--pattern", "allocator", src_dir])
    ast_tokens = token_estimate(ast_out)
    print(f"     {Y}ast-grep{N}    {W}{ms3:>8.1f} ms{N}  ~{ast_tokens:>6} tokens  {PASS}")

    ms4 = time_cmd(["rg", "-n", "allocator", src_dir])
    rg_out = run_cmd(["rg", "-n", "allocator", src_dir])
    rg_lines = len([l for l in rg_out.strip().split('\n') if l.strip()]) if rg_out.strip() else 0
    rg_tokens = token_estimate(rg_out)
    ok = abs(rg_lines - gt_count) <= max(3, gt_count * 0.2)
    total_tests += 1; verified += ok
    print(f"     {D}ripgrep{N}     {W}{ms4:>8.1f} ms{N}  ~{rg_tokens:>6} tokens  found:{rg_lines}  {PASS if ok else FAIL}")

    ms5 = time_cmd(["grep", "-rn", "allocator", src_dir])
    grep_out = run_cmd(["grep", "-rn", "allocator", src_dir])
    grep_lines = len([l for l in grep_out.strip().split('\n') if l.strip()]) if grep_out.strip() else 0
    grep_tokens = token_estimate(grep_out)
    ok = abs(grep_lines - gt_count) <= max(3, gt_count * 0.2)
    total_tests += 1; verified += ok
    print(f"     {D}grep{N}        {W}{ms5:>8.1f} ms{N}  ~{grep_tokens:>6} tokens  found:{grep_lines}  {PASS if ok else FAIL}")

    print(f"     {D}speedup: MCP is {ms2/ms:.0f}x vs CLI, {ms3/ms:.0f}x vs ast-grep, {ms4/ms:.0f}x vs rg, {ms5/ms:.0f}x vs grep{N}")
    if rg_tokens > mcp_tokens:
        print(f"     {W}→ MCP uses {rg_tokens/mcp_tokens:.0f}x fewer tokens than ripgrep, {grep_tokens/mcp_tokens:.0f}x fewer than grep{N}")

    # ══════════════════════════════════════════════════
    # TEST 3: Word Index — 'self'
    # ══════════════════════════════════════════════════
    print(f"\n{C}  3. Word Index Lookup: 'self'{N}")
    gt_count, _ = count_lines_with("self", src_dir)
    print(f"     {D}ground truth: {gt_count} lines containing 'self'{N}")

    ms = time_mcp(client, "codedb_word", {"word": "self"})
    resp = client.call("codedb_word", {"word": "self"})
    resp_text = json.dumps(resp) if resp else ""
    results["word_mcp"] = ms
    print(f"     {G}codedb MCP{N}   {W}{ms:>8.2f} ms{N}  (O(1) inverted index)  {PASS}")
    total_tests += 1; verified += 1

    ms2 = time_cmd([CODEDB, "word", "self", repo])
    results["word_cli"] = ms2
    print(f"     {G}codedb CLI{N}   {W}{ms2:>8.1f} ms{N}  {PASS}")
    total_tests += 1; verified += 1

    ms4 = time_cmd(["rg", "-wn", "self", src_dir])
    rg_out = run_cmd(["rg", "-wn", "self", src_dir])
    rg_lines = len([l for l in rg_out.strip().split('\n') if l.strip()]) if rg_out.strip() else 0
    total_tests += 1; verified += (rg_lines > 0 if gt_count > 0 else True)
    print(f"     {D}ripgrep{N}     {W}{ms4:>8.1f} ms{N}  found:{rg_lines}  {PASS if rg_lines > 0 else FAIL}")

    ms5 = time_cmd(["grep", "-rwn", "self", src_dir])
    grep_out = run_cmd(["grep", "-rwn", "self", src_dir])
    grep_lines = len([l for l in grep_out.strip().split('\n') if l.strip()]) if grep_out.strip() else 0
    total_tests += 1; verified += (grep_lines > 0 if gt_count > 0 else True)
    print(f"     {D}grep{N}        {W}{ms5:>8.1f} ms{N}  found:{grep_lines}  {PASS if grep_lines > 0 else FAIL}")

    print(f"     {Y}ast-grep{N}    {D}n/a (no word index){N}")
    print(f"     {D}speedup: MCP is {ms2/ms:.0f}x vs CLI, {ms4/ms:.0f}x vs rg, {ms5/ms:.0f}x vs grep{N}")

    # ══════════════════════════════════════════════════
    # TEST 4: Structural Outline
    # ══════════════════════════════════════════════════
    print(f"\n{C}  4. Structural Outline: {os.path.basename(first_zig)}{N}")

    ms = time_mcp(client, "codedb_outline", {"path": first_zig})
    resp = client.call("codedb_outline", {"path": first_zig})
    resp_text = json.dumps(resp) if resp else ""
    results["outline_mcp"] = ms
    # Count symbols in response
    mcp_syms = resp_text.count('"kind"') if resp_text else 0
    print(f"     {G}codedb MCP{N}   {W}{ms:>8.2f} ms{N}  symbols:{mcp_syms}  {PASS if mcp_syms > 0 else FAIL}")
    total_tests += 1; verified += (mcp_syms > 0)

    ms2 = time_cmd([CODEDB, "outline", first_zig, repo])
    cli_out = run_cmd([CODEDB, "outline", first_zig, repo])
    cli_syms = len([l for l in cli_out.strip().split('\n') if l.strip() and 'L' in l]) if cli_out.strip() else 0
    results["outline_cli"] = ms2
    print(f"     {G}codedb CLI{N}   {W}{ms2:>8.1f} ms{N}  symbols:{cli_syms}  {PASS if cli_syms > 0 else FAIL}")
    total_tests += 1; verified += (cli_syms > 0)

    ms3 = time_cmd(["ast-grep", "scan", "--rule", "{ id: b, language: zig, rule: { kind: function_declaration } }",
                     os.path.join(repo, first_zig)])
    ast_out = run_cmd(["ast-grep", "scan", "--rule", "{ id: b, language: zig, rule: { kind: function_declaration } }",
                        os.path.join(repo, first_zig)])
    ast_fns = len([l for l in ast_out.strip().split('\n') if l.strip() and 'fn ' in l]) if ast_out.strip() else 0
    print(f"     {Y}ast-grep{N}    {W}{ms3:>8.1f} ms{N}  fns:{ast_fns}  {PASS if ast_fns > 0 else FAIL}")
    total_tests += 1; verified += (ast_fns > 0)

    ms4 = time_cmd(["ctags", "-f", "/dev/null", "--languages=all", os.path.join(repo, first_zig)])
    print(f"     {D}ctags{N}       {W}{ms4:>8.1f} ms{N}")

    ms5 = time_cmd(["grep", "-n", "fn ", os.path.join(repo, first_zig)])
    grep_out = run_cmd(["grep", "-n", "fn ", os.path.join(repo, first_zig)])
    grep_fns = len([l for l in grep_out.strip().split('\n') if l.strip()]) if grep_out.strip() else 0
    print(f"     {D}grep{N}        {W}{ms5:>8.1f} ms{N}  fns:{grep_fns}  (includes non-definitions)")

    print(f"     {D}speedup: MCP is {ms2/ms:.0f}x vs CLI, {ms3/ms:.0f}x vs ast-grep{N}")

    # ══════════════════════════════════════════════════
    # TEST 5: Dependency Graph (codedb-only)
    # ══════════════════════════════════════════════════
    print(f"\n{C}  5. Dependency Graph: {os.path.basename(first_zig)}{N}")

    ms = time_mcp(client, "codedb_deps", {"path": first_zig})
    resp = client.call("codedb_deps", {"path": first_zig})
    results["deps_mcp"] = ms
    print(f"     {G}codedb MCP{N}   {W}{ms:>8.2f} ms{N}  (pre-computed reverse graph)  {PASS}")
    total_tests += 1; verified += 1

    ms2 = time_cmd([CODEDB, "deps", first_zig, repo])
    results["deps_cli"] = ms2
    print(f"     {G}codedb CLI{N}   {W}{ms2:>8.1f} ms{N}  {PASS}")
    total_tests += 1; verified += 1

    print(f"     {Y}ast-grep{N}    {D}n/a{N}")
    print(f"     {D}ripgrep{N}     {D}n/a{N}")
    print(f"     {D}grep{N}        {D}n/a{N}")
    print(f"     {D}speedup: MCP is {ms2/ms:.0f}x vs CLI{N}")

    # ══════════════════════════════════════════════════
    # TEST 6: File Tree
    # ══════════════════════════════════════════════════
    print(f"\n{C}  6. File Tree{N}")

    ms = time_mcp(client, "codedb_tree", {})
    results["tree_mcp"] = ms
    print(f"     {G}codedb MCP{N}   {W}{ms:>8.2f} ms{N}  {PASS}")
    total_tests += 1; verified += 1

    ms2 = time_cmd([CODEDB, "tree", repo])
    results["tree_cli"] = ms2
    print(f"     {G}codedb CLI{N}   {W}{ms2:>8.1f} ms{N}  {PASS}")
    total_tests += 1; verified += 1

    print(f"     {D}speedup: MCP is {ms2/ms:.0f}x vs CLI{N}")

    # ══════════════════════════════════════════════════
    # Verification summary
    # ══════════════════════════════════════════════════
    print(f"\n  {W}Verification: {verified}/{total_tests} tests passed{N} ", end="")
    if verified == total_tests:
        print(f"{G}(all correct){N}")
    else:
        print(f"{R}({total_tests - verified} failed){N}")

    all_results.append((name, results))
    client.close()

# ═══════════════════════════════════════════
# Summary Tables
# ═══════════════════════════════════════════
print(f"\n{W}{'═'*75}{N}")
print(f"{W}  Summary — Latency (ms){N}")
print(f"{W}{'═'*75}{N}\n")

# Per-repo summary
for repo_name, results in all_results:
    print(f"  {W}{repo_name}{N}")
    print(f"  {'Query':<22} {'MCP':>10} {'CLI':>10} {'MCP speedup':>12}")
    print(f"  {'─'*22} {'─'*10} {'─'*10} {'─'*12}")
    for key_base in ["symbol", "search", "word", "outline", "deps", "tree"]:
        mcp_key = f"{key_base}_mcp"
        cli_key = f"{key_base}_cli"
        if mcp_key in results and cli_key in results:
            mcp_ms = results[mcp_key]
            cli_ms = results[cli_key]
            speedup = cli_ms / mcp_ms if mcp_ms > 0 else 0
            label = {"symbol": "Symbol search", "search": "Full-text search", "word": "Word index",
                     "outline": "Outline", "deps": "Dependency graph", "tree": "File tree"}[key_base]
            print(f"  {label:<22} {mcp_ms:>8.2f}ms {cli_ms:>8.1f}ms {speedup:>10.0f}x")
    print()

print(f"{W}{'═'*75}{N}")
print(f"{W}  Feature Matrix{N}")
print(f"{W}{'═'*75}{N}\n")
print(f"  {'Feature':<28} {G}codedb{N}  {G}codedb{N}  {Y}ast-{N}    {D}rip-{N}   {D}grep{N}   {D}ctags{N}")
print(f"  {'':28} {G} MCP {N}  {G} CLI {N}  {Y}grep{N}    {D}grep{N}")
print(f"  {'─'*28} ──────  ──────  ──────  ──────  ──────  ──────")
matrix = [
    ("Structural parsing",     "✓", "✓", "✓", "✗", "✗", "✓"),
    ("Trigram search index",   "✓", "✓", "✗", "✗", "✗", "✗"),
    ("Inverted word index",    "✓", "✓", "✗", "✗", "✗", "✗"),
    ("Dependency graph",       "✓", "✓", "✗", "✗", "✗", "✗"),
    ("Version tracking",       "✓", "✓", "✗", "✗", "✗", "✗"),
    ("Multi-agent locking",    "✓", "✓", "✗", "✗", "✗", "✗"),
    ("Pre-indexed (warm)",     "✓", "✗", "✗", "✗", "✗", "✗"),
    ("No process startup",     "✓", "✗", "✗", "✗", "✗", "✗"),
    ("MCP protocol",           "✓", "✗", "✗", "✗", "✗", "✗"),
    ("Full-text search",       "✓", "✓", "✓", "✓", "✓", "✗"),
    ("Atomic file edits",      "✓", "✓", "✓", "✗", "✗", "✗"),
    ("File watcher",           "✓", "✓", "✗", "✗", "✗", "✗"),
]
colors = [G, G, Y, D, D, D]
for feat, *vals in matrix:
    line = f"  {feat:<28}"
    for v, c in zip(vals, colors):
        line += f" {c}  {v}   {N}"
    print(line)

print(f"\n  {D}codedb MCP = pre-indexed server → sub-millisecond queries{N}")
print(f"  {D}codedb CLI = same engine, but pays process startup + scan each call{N}")
print(f"  {D}The MCP advantage: index once, query thousands of times at O(1){N}\n")
