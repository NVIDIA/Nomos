# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build the documentation diagrams as light and dark SVGs.

Standard library only. Run from anywhere:

    python3 docs/assets/diagrams/build_diagrams.py

Writes <name>.svg and <name>-dark.svg next to this file. Markdown pages embed
them with <picture> so GitHub picks the variant matching the reader's theme.
Every label is checked against the code it describes; keep them in sync when
the request path, the Writ fields or the configuration keys change.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent
FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# GitHub Primer colours, so the diagrams sit naturally on github.com.
THEMES = {
    "light": {
        "bg": "#ffffff",
        "panel": "#f6f8fa",
        "box": "#ffffff",
        "text": "#1f2328",
        "muted": "#59636e",
        "line": "#d1d9e0",
        "edge": "#818b98",
        "blue": "#0969da",
        "blue_bg": "#ddf4ff",
        "red": "#cf222e",
        "red_bg": "#ffebe9",
        "green": "#1a7f37",
        "green_bg": "#dafbe1",
        "orange": "#bc4c00",
        "orange_bg": "#fff1e5",
        "purple": "#8250df",
        "purple_bg": "#fbefff",
    },
    "dark": {
        "bg": "#0d1117",
        "panel": "#151b23",
        "box": "#0d1117",
        "text": "#e6edf3",
        "muted": "#9198a1",
        "line": "#3d444d",
        "edge": "#656c76",
        "blue": "#4493f8",
        "blue_bg": "#0d2847",
        "red": "#f85149",
        "red_bg": "#3c1618",
        "green": "#3fb950",
        "green_bg": "#0f2d1a",
        "orange": "#f0883e",
        "orange_bg": "#3b2210",
        "purple": "#ab7df8",
        "purple_bg": "#2a1f45",
    },
}


class Svg:
    """Tiny SVG writer with theme colour names."""

    def __init__(self, width: int, height: int, theme: dict[str, str]) -> None:
        self.w, self.h, self.t = width, height, theme
        self.parts: list[str] = []
        markers = "".join(
            f"<marker id='arrow-{name}' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' "
            f"markerHeight='7' orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' "
            f"fill='{theme[name]}'/></marker>"
            for name in ("edge", "blue", "red", "green", "purple")
        )
        self.parts.append(f"<defs>{markers}</defs>")
        self.parts.append(f"<rect width='{width}' height='{height}' fill='{theme['bg']}'/>")

    def c(self, name: str) -> str:
        return self.t.get(name, name)

    def rect(self, x, y, w, h, fill="box", stroke="line", rx=6, dash=False, width=1.0):
        d = " stroke-dasharray='5 4'" if dash else ""
        self.parts.append(
            f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='{rx}' fill='{self.c(fill)}' "
            f"stroke='{self.c(stroke)}' stroke-width='{width}'{d}/>"
        )

    def text(
        self, x, y, s, size=12, color="text", weight=400, anchor="start", mono=False, rotate=None
    ):
        family = MONO if mono else FONT
        rot = f" transform='rotate({rotate} {x} {y})'" if rotate is not None else ""
        self.parts.append(
            f"<text x='{x}' y='{y}' font-family=\"{family}\" font-size='{size}' "
            f"font-weight='{weight}' fill='{self.c(color)}' text-anchor='{anchor}'{rot}>"
            f"{escape(s)}</text>"
        )

    def path(self, d, color="edge", width=1.5, arrow=None, dash=False, halo=False):
        if halo:  # background-coloured underlay so a crossing reads as a bridge
            self.parts.append(
                f"<path d='{d}' fill='none' stroke='{self.c('bg')}' stroke-width='{width + 6}'/>"
            )
        a = f" marker-end='url(#arrow-{arrow})'" if arrow else ""
        ds = " stroke-dasharray='5 4'" if dash else ""
        self.parts.append(
            f"<path d='{d}' fill='none' stroke='{self.c(color)}' stroke-width='{width}' "
            f"stroke-linejoin='round'{ds}{a}/>"
        )

    def badge(self, cx, cy, n, color):
        self.parts.append(f"<circle cx='{cx}' cy='{cy}' r='10' fill='{self.c(color)}'/>")
        self.text(cx, cy + 4, str(n), size=11, color="bg", weight=700, anchor="middle")

    def save(self, name: str, title: str) -> None:
        body = "".join(self.parts)
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {self.w} {self.h}' "
            f"width='{self.w}' height='{self.h}' role='img'>"
            f"<title>{escape(title)}</title>{body}</svg>\n"
        )
        (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")


def step(s: Svg, x, y, w, n, title, detail, accent="edge", tint="box"):
    """A numbered step: badge, bold title, one muted detail line."""
    s.rect(x, y, w, 40, fill=tint, stroke=accent if accent != "edge" else "line")
    s.badge(x + 18, y + 20, n, accent if accent != "edge" else "muted")
    s.text(x + 36, y + 17, title, size=12.5, weight=600)
    s.text(x + 36, y + 32, detail, size=11, color="muted")


def lane(s: Svg, x, y, w, h, title, subtitle):
    s.rect(x, y, w, h, fill="panel", stroke="line", rx=10, dash=True)
    s.text(x + 14, y + 22, title, size=12, weight=700, color="text")
    s.text(x + 14, y + 38, subtitle, size=11, color="muted")


# ── 1. Request lifecycle ────────────────────────────────────────────────────
# Order follows council_authorize in nomos/nat/functions.py (authenticate,
# certified evidence, permissions, OPA, chairman + members, mint) and the
# executor in executors/docker/{api,enforcer}.py (agent binding, signature,
# expiry, nonce claim, local checks, files, run, signed result).


def request_lifecycle(theme: dict[str, str]) -> Svg:
    s = Svg(1040, 700, theme)
    s.text(16, 26, "One request through Nomos", size=17, weight=700)
    s.text(
        16,
        46,
        "Nothing runs until Nomos approves it. The executor runs only what was signed, "
        "then signs what happened.",
        size=12,
        color="muted",
    )

    top, pitch = 136, 50
    row = lambda i: top + (i - 1) * pitch  # noqa: E731

    lane(s, 16, 66, 376, 618, "NOMOS COUNCIL SERVICE", "decides; never runs anything")
    lane(s, 420, 66, 192, 618, "YOUR AGENT", "carries the Writ")
    lane(s, 640, 66, 384, 618, "EXECUTOR", "holds the tools' credentials")

    # Nomos steps 1-7, joined by a timeline spine through the badges.
    nx, nw = 30, 348
    s.path(f"M{nx + 18} {row(1) + 20}V{row(7) + 20}", color="line", width=2)
    nomos = [
        (1, "Authenticate", "agent token · council membership", "edge", "box"),
        (2, "Verify receipts", "signatures on attached certified_evidence", "edge", "box"),
        (3, "Check permissions", "action scopes vs. the account's role", "edge", "box"),
        (4, "Policy (OPA)", "deterministic rules · fails closed if unreachable", "edge", "box"),
        (
            5,
            "Assess risk (chairman)",
            "sets risk level and the approval threshold",
            "orange",
            "orange_bg",
        ),
        (
            6,
            "Council vote (members)",
            "each member reviews; unanimous by default",
            "orange",
            "orange_bg",
        ),
        (
            7,
            "Sign the Writ",
            "Ed25519 · lifetime 30\u2013300 s, shorter for higher risk",
            "blue",
            "blue_bg",
        ),
    ]
    for n, title, detail, accent, tint in nomos:
        step(s, nx, row(n), nw, n, title, detail, accent, tint)

    # Executor steps 8-12 start where the Writ arrives (row 7).
    ex, ew = 654, 250
    s.path(f"M{ex + 18} {row(7) + 20}V{row(11) + 20}", color="line", width=2)
    execu = [
        (8, "Verify", "target agent · signature · expiry", "edge", "box"),
        (9, "Claim the nonce", "atomic · single use · never released", "edge", "box"),
        (10, "Local checks", "dirs · tools · args · paths · patterns", "edge", "box"),
        (11, "Run", "packaged files, then the exact command", "edge", "box"),
        (12, "Sign a receipt", "success or rejection, executor's own key", "green", "green_bg"),
    ]
    for i, (n, title, detail, accent, tint) in enumerate(execu):
        step(s, ex, row(7 + i), ew, n, title, detail, accent, tint)

    # Protected tools sit behind the executor, next to "Run".
    s.rect(918, row(10), 92, 40, fill="box", stroke="text", width=1.25)
    s.text(964, row(10) + 17, "Protected", size=11.5, weight=600, anchor="middle")
    s.text(964, row(10) + 31, "tools", size=11.5, weight=600, anchor="middle")
    s.path(f"M{ex + ew} {row(10) + 20}H916", color="edge", arrow="edge")

    # Agent cards.
    ax, aw = 432, 152
    s.rect(ax, row(1), aw, 182, fill="box", stroke="text", width=1.25)
    s.text(ax + 10, row(1) + 18, "Propose an action", size=12.5, weight=600)
    s.text(ax + 10, row(1) + 36, "council_authorize", size=11, mono=True, color="blue")
    for i, field in enumerate(
        [
            "command",
            "working_directory",
            "description",
            "justification",
            "blast_radius",
            "certified_evidence",
            "agent_id, auth_token",
        ]
    ):
        s.text(ax + 16, row(1) + 56 + i * 17, field, size=10.5, mono=True, color="muted")

    s.rect(ax, row(6) - 16, aw, 54, fill="red_bg", stroke="red")
    s.text(ax + 10, row(6) + 2, "Denied: revise", size=12.5, weight=600, color="red")
    s.text(ax + 10, row(6) + 18, "guidance explains why;", size=10.5, color="muted")
    s.text(ax + 10, row(6) + 31, "a revision is a new request", size=10.5, color="muted")

    s.rect(ax, row(7) + 6, aw, 58, fill="blue_bg", stroke="blue")
    s.text(ax + 10, row(7) + 24, "Hand off the Writ", size=12.5, weight=600, color="blue")
    s.text(ax + 10, row(7) + 40, "POST /v1/writs/execute", size=10, mono=True, color="muted")
    s.text(ax + 10, row(7) + 55, "header X-Agent-ID", size=10, mono=True, color="muted")

    s.rect(ax, row(11), aw, 40, fill="green_bg", stroke="green")
    s.text(ax + 10, row(11) + 17, "Keep the receipt", size=12.5, weight=600, color="green")
    s.text(ax + 10, row(11) + 32, "signed ExecutionResult", size=10, mono=True, color="muted")

    # Receipt loop: agent attaches the receipt to its next request (step 2).
    loop_x = 598
    s.path(
        f"M{ax + aw - 20} {row(11)}V{row(11) - 12}H{loop_x}V{row(1) + 150}H{ax + aw + 2}",
        color="purple",
        dash=True,
        arrow="purple",
        width=1.5,
    )
    s.text(
        loop_x + 9,
        row(4) + 14,
        "next request: certified_evidence",
        size=10.5,
        color="purple",
        anchor="middle",
        rotate=-90,
    )

    # Request into step 1.
    s.path(f"M{ax} {row(1) + 14}H{nx + nw + 2}", color="text", arrow="edge", width=1.5)
    s.text(405, row(1) + 8, "Intent", size=10.5, color="muted", anchor="middle")

    # Deny rail from steps 1-6 to the agent.
    rail = 404
    for n in range(1, 7):
        s.path(f"M{nx + nw} {row(n) + 28}H{rail}", color="red", width=1.25)
    s.path(f"M{rail} {row(1) + 28}V{row(6) + 28}", color="red", width=1.5)
    s.path(f"M{rail} {row(6) + 11}H{ax - 2}", color="red", arrow="red", width=1.5)

    # Approved: Writ back to the agent, then on to the executor.
    s.path(
        f"M{nx + nw} {row(7) + 20}H{rail - 10}V{row(7) + 26}H{ax - 2}",
        color="blue",
        arrow="blue",
        width=2,
    )
    s.text(404, row(7) + 48, "Writ", size=10.5, color="blue", anchor="middle", weight=600)
    s.path(f"M{ax + aw} {row(7) + 26}H{ex - 2}", color="blue", arrow="blue", width=2, halo=True)
    s.text(603, row(7) + 20, "Writ", size=10.5, color="blue", weight=600)

    # Receipt back to the agent.
    s.path(f"M{ex} {row(11) + 20}H{ax + aw + 2}", color="green", arrow="green", width=2)

    # What the Writ signs, under the step that signs it.
    wy = row(8)
    s.rect(nx, wy, nw, 196, fill="box", stroke="blue", rx=6)
    s.text(nx + 12, wy + 20, "What the Writ signs", size=12.5, weight=600, color="blue")
    signed = [
        ("command", "the exact string that will run"),
        ("working_directory", "where it runs, on the executor"),
        ("packaged_files", "path, contents, sha256, mode"),
        ("target_agent_id", "the only agent that may use it"),
        ("nonce, writ_id", "single use"),
        ("issued_at, expires_at", "validity window"),
        ("council_id, intent_hash", "which council, which request"),
        ("threshold_used, risk", "how it was decided"),
    ]
    for i, (k, v) in enumerate(signed):
        y = wy + 40 + i * 18
        s.text(nx + 14, y, k, size=10.5, mono=True)
        s.text(nx + 196, y, v, size=10.5, color="muted")
    s.text(nx + 12, wy + 188, "member_votes ride along unsigned.", size=10.5, color="muted")

    # Legend, top-right.
    lx, ly = 654, 118
    s.rect(lx, ly, 356, 198, fill="box", stroke="line")
    s.text(lx + 12, ly + 20, "Reading the diagram", size=12.5, weight=600)
    legend = [
        ("blue", False, "approved path: the signed Writ"),
        ("red", False, "denied: status, guidance, missing_permissions"),
        ("green", False, "signed receipt (ExecutionResult)"),
        ("purple", True, "receipt attached to the next request"),
    ]
    for i, (color, dash, label) in enumerate(legend):
        y = ly + 42 + i * 22
        s.path(f"M{lx + 14} {y - 4}H{lx + 52}", color=color, width=2, arrow=color, dash=dash)
        s.text(lx + 62, y, label, size=11, color="text")
    s.rect(lx + 14, ly + 128, 38, 18, fill="orange_bg", stroke="orange", rx=4)
    s.text(lx + 62, ly + 141, "Security Council: chairman + voting members", size=11)
    s.rect(lx + 14, ly + 156, 38, 18, fill="panel", stroke="line", rx=4, dash=True)
    s.text(lx + 62, ly + 169, "trust boundary; each side checks the other", size=11)
    s.text(
        lx + 12,
        ly + 190,
        "Steps 5 and 6 run concurrently in the shipped config.",
        size=10.5,
        color="muted",
    )
    s.rect(lx, 330, 356, 84, fill="box", stroke="line")
    s.text(lx + 12, 350, "Approval is not execution", size=12.5, weight=600)
    s.text(
        lx + 12,
        368,
        "Steps 8\u201310 run on every Writ. A council-approved command",
        size=11,
        color="muted",
    )
    s.text(
        lx + 12,
        384,
        "can still be refused at step 10, and its Writ is then spent.",
        size=11,
        color="muted",
    )
    s.text(
        lx + 12,
        400,
        "HTTP 200 alone is not success: read success and stage.",
        size=11,
        color="muted",
    )
    return s


# ── 2. Agent adapter ────────────────────────────────────────────────────────
# Matches the five steps in docs/integrate-your-agent.md.


def agent_adapter(theme: dict[str, str]) -> Svg:
    s = Svg(1040, 400, theme)
    s.text(16, 26, "Adapt your agent: one wrapper around its tool calls", size=17, weight=700)
    s.text(
        16,
        46,
        "Your code, roughly 40 lines. Nomos and the executor are services it calls; "
        "neither calls into your agent.",
        size=12,
        color="muted",
    )

    # External services above the adapter.
    s.rect(352, 66, 200, 62, fill="panel", stroke="line", rx=10, dash=True)
    s.text(452, 90, "Nomos council", size=12.5, weight=700, anchor="middle")
    s.text(
        452,
        108,
        "MCP: /mcp (Bearer proxy token)",
        size=10.5,
        color="muted",
        anchor="middle",
        mono=True,
    )
    s.rect(686, 66, 200, 62, fill="panel", stroke="line", rx=10, dash=True)
    s.text(786, 90, "Executor", size=12.5, weight=700, anchor="middle")
    s.text(
        786, 108, "HTTP: /v1/writs/execute", size=10.5, color="muted", anchor="middle", mono=True
    )

    # Your agent, left.
    s.rect(16, 176, 130, 92, fill="box", stroke="text", width=1.25)
    s.text(81, 204, "Your agent", size=13, weight=700, anchor="middle")
    s.text(81, 222, "any framework,", size=11, color="muted", anchor="middle")
    s.text(81, 238, "any model", size=11, color="muted", anchor="middle")
    s.text(81, 256, "wants to run", size=10.5, color="muted", anchor="middle")

    # Adapter zone with five steps.
    s.rect(166, 150, 862, 144, fill="panel", stroke="blue", rx=10, dash=True)
    s.text(180, 170, "NOMOS ADAPTER (your code)", size=12, weight=700, color="blue")
    bw, gap, bx0, by = 160, 10, 180, 184
    steps = [
        (
            1,
            "Intercept",
            [
                "map the call to fields:",
                "command, working_directory",
                "justification, blast_radius",
            ],
        ),
        (
            2,
            "Authorize",
            ["council_authorize(...)", "agent_id, auth_token,", "council_id, evidence"],
        ),
        (3, "Decide", ['status == "approved"?', "no: return guidance", "yes: take the writ"]),
        (4, "Execute", ['POST {"writ": ...}', "header X-Agent-ID", "check success, stage"]),
        (
            5,
            "Keep receipt",
            ["full ExecutionResult,", "signed by the executor;", "never edit its output"],
        ),
    ]
    centers = []
    for i, (n, title, lines) in enumerate(steps):
        x = bx0 + i * (bw + gap)
        centers.append(x + bw / 2)
        accent = {3: "orange", 4: "blue", 5: "green"}.get(n, "muted")
        s.rect(x, by, bw, 96, fill="box", stroke="line")
        s.badge(x + 18, by + 18, n, accent)
        s.text(x + 34, by + 23, title, size=12.5, weight=600)
        for j, ln in enumerate(lines):
            s.text(
                x + 10,
                by + 46 + j * 16,
                ln,
                size=10.5,
                color="muted",
                mono=(j == 0 and n in (2, 4)),
            )
        if i:
            s.path(f"M{x - gap} {by + 48}H{x - 2}", color="edge", arrow="edge")

    s.path(f"M146 {by + 48}H{bx0 - 2}", color="text", arrow="edge", width=1.5)

    # Council and executor exchanges.
    c2, c4 = centers[1], centers[3]
    s.path(f"M{c2 - 24} {by}V130", color="edge", arrow="edge")
    s.text(c2 - 30, 146, "action + tokens", size=10, color="muted", anchor="end")
    s.path(f"M{c2 + 24} 130V{by - 2}", color="blue", arrow="blue")
    s.text(c2 + 30, 146, "decision + writ", size=10, color="blue")
    s.path(f"M{c4 - 24} {by}V130", color="blue", arrow="blue")
    s.text(c4 - 30, 146, "writ", size=10, color="blue", anchor="end")
    s.path(f"M{c4 + 24} 130V{by - 2}", color="green", arrow="green")
    s.text(c4 + 30, 146, "signed result", size=10, color="green")

    # Denied: guidance back to the agent. Receipt: attached to the next request.
    c3, c5 = centers[2], centers[4]
    s.path(f"M{c3} {by + 96}V318H104V{270}", color="red", arrow="red", width=1.5)
    s.text(
        c3 - 8,
        334,
        "denied: guidance goes back; the agent revises and asks again",
        size=10.5,
        color="red",
        anchor="end",
    )
    s.path(f"M{c5} {by + 96}V356H52V{270}", color="purple", arrow="purple", width=1.5, dash=True)
    s.text(
        c5 - 8,
        372,
        "the receipt rides in certified_evidence on the next request",
        size=10.5,
        color="purple",
        anchor="end",
    )
    return s


# ── 3. What you can swap ────────────────────────────────────────────────────
# Keys from nomos/config/settings.py, configs/council.yaml,
# configs/local_council_catalog.yaml and deploy/executor-*.yaml.


def modularity(theme: dict[str, str]) -> Svg:
    s = Svg(1040, 470, theme)
    s.text(16, 26, "What you can swap without forking", size=17, weight=700)
    s.text(
        16,
        46,
        "Fixed contracts in the middle; everything around them is configuration "
        "or a service you supply.",
        size=12,
        color="muted",
    )

    def module(x, y, w, title, key, note, h=58):
        s.rect(x, y, w, h, fill="box", stroke="line")
        s.text(x + 10, y + 18, title, size=12.5, weight=600)
        s.text(x + 10, y + 35, key, size=10.5, mono=True, color="blue")
        s.text(x + 10, y + 50, note, size=10.5, color="muted")

    lane(s, 16, 66, 232, 388, "YOUR SIDE", "bring your own")
    module(28, 112, 208, "Agent and framework", "any MCP or gRPC client", "keeps its own tools")
    module(28, 180, 208, "Transport", "MCP /mcp · gRPC :9904", "gRPC maps the core tools")
    module(
        28,
        248,
        208,
        "Accounts and roles",
        "agent_accounts create --role",
        "readonly · developer · ...",
    )
    module(
        28, 316, 208, "Which council", "council_id per request", "catalog: local_council_catalog"
    )

    lane(s, 276, 66, 476, 388, "NOMOS COUNCIL", "configs/council.yaml, one file per council")
    cw = 222
    module(
        290,
        112,
        cw,
        "Voting members",
        "members[]: priorities, weight",
        "any number; own prompt each",
    )
    module(
        520,
        112,
        cw,
        "Models per role",
        "llm_backends.<name>.provider",
        "nim · openai · anthropic · nv_inference",
    )
    module(
        290,
        180,
        cw,
        "Chairman and threshold",
        "chairman.allow_quorum",
        "unanimous unless quorum allowed",
    )
    module(
        520,
        180,
        cw,
        "Self-hosted models",
        "llm_backends.<name>.base_url",
        "local NIM or compatible endpoint",
    )
    module(
        290,
        248,
        cw,
        "Policy rules",
        "opa.policy_id · policies/*.rego",
        "or an external OPA via opa.url",
    )
    module(520, 248, cw, "Code review of files", "code_review.enabled", "advisory, before the vote")
    module(
        290,
        316,
        cw,
        "Durable storage",
        "storage.mode: redis | postgres",
        "Redis still holds nonces",
    )
    module(520, 316, cw, "Signing keys", "signing.provider: local | vault", "Vault transit in Kind")

    lane(s, 780, 66, 244, 388, "EXECUTION", "bundled or your own")
    module(
        792, 112, 220, "Executor service", "deploy/executor-*.yaml", "OpenShell (default), Docker"
    )
    module(792, 180, 220, "Your own executor", "POST /v1/writs/execute", "implement the contract")
    module(792, 248, 220, "Local policy", "tools.allowed · denied_paths", "plus blocked_patterns")
    module(792, 316, 220, "Runtime backend", "openshell.endpoint", "where commands actually run")

    # Fixed contracts between the zones.
    def contract(x, y, label, sub, color):
        s.rect(x - 4, y, 36, 70, fill=f"{color}_bg", stroke=color, rx=8)
        s.text(
            x + 14, y + 35, label, size=10.5, weight=700, color=color, anchor="middle", rotate=-90
        )
        s.text(x + 26, y + 35, sub, size=9, color=color, anchor="middle", rotate=-90, mono=True)

    contract(246, 150, "Intent", "tool args", "blue")
    contract(750, 112, "Writ", "Ed25519", "blue")
    contract(750, 248, "Receipt", "signed", "green")

    s.rect(290, 392, 452, 50, fill="panel", stroke="line", rx=6)
    s.text(302, 412, "Fixed in every deployment", size=12, weight=600)
    s.text(
        302,
        430,
        "authentication · Writ format and signature · single-use nonce · signed receipts",
        size=10.5,
        color="muted",
    )
    s.rect(28, 392, 208, 50, fill="panel", stroke="line", rx=6)
    s.text(40, 412, "Reference", size=12, weight=600)
    s.text(40, 430, "docs/reference/configuration.md", size=10.5, mono=True, color="muted")
    s.rect(792, 392, 220, 50, fill="panel", stroke="line", rx=6)
    s.text(804, 412, "Contract", size=12, weight=600)
    s.text(804, 430, "trusted_executor_contract/", size=10.5, mono=True, color="muted")
    return s


DIAGRAMS = {
    "request-lifecycle": (request_lifecycle, "Nomos request lifecycle"),
    "agent-adapter": (agent_adapter, "Adapting an agent to Nomos"),
    "modularity": (modularity, "What you can swap in Nomos"),
}

if __name__ == "__main__":
    for name, (build, title) in DIAGRAMS.items():
        for theme_name, theme in THEMES.items():
            suffix = "" if theme_name == "light" else "-dark"
            build(theme).save(name + suffix, title)
            print(OUT / f"{name}{suffix}.svg")
