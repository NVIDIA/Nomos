# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Architecture figures for README.md, DESIGN.md and docs/architecture (matplotlib). Run: `make docs`.

Each function draws one figure into docs/figures/ and returns its path; `python docs/build/figures.py NAME …` draws
only the named ones. Example values are read from docs/architecture/examples/ (recorded from real runs; see
docs/architecture/examples/README.md), never typed in.

Each figure answers one question, stated in its title, and leaves the details to the Markdown beside it. `save`
enforces the density rules: at most 2600 px tall at 1560 px wide, no text under 7 pt (23 px at the source), no text
block longer than two lines (counting the rows of a card or panel together), at most eight legend entries, and the
overview within 1560 x 1100 px, twelve boxes and sixty words.

One visual language, drawn by the helpers below and explained by the same legend wording in every figure (each
figure's legend lists only the encodings it uses):

- Colour is authority, never state. A party's box edge, or a field's colour, says whose word it is: O the Relay host
  and the plugin's code (what the call actually carries), A asserted (unverified, like the model's intent), N Nomos
  (Council, Executor), P the plugin's store and what it signs; grey = the application and the operator's host (no
  Nomos code). A party box carries its name; a field (council-request, lineage) carries the one-letter tag as well,
  for readers who cannot tell the colours apart, and only those figures show tags in their legend. The palette keeps
  every pair apart under deuteranopia.
- A shaded box without a border is a stage, a state or a fact, not a party: its colour means nothing. A hatched box
  is an item nobody signs.
- A dashed rounded outline is only ever a zone: a process, a host or a deployment.
- Arrows start at the party that initiates. A dark solid arrow runs on every call; a light open-headed one is the
  reply on the same connection; a dashed one runs once (install or start). Every arrow between parties carries its
  connection id (C1–C8): a filled disc for per-call links, a hollow one for once-only links.
- An outcome always has the same mark: → passthrough, ⊘ refused by the plugin, ✕ the Council said no or failed,
  ✓ dispatched and answered, ? may have run (outcome unknown), ↺ replayed.

`save` refuses a figure that breaks a rule above, has text overflowing its box or overlapping text, and reports any
arrow that crosses text, so the figures stay legible at README width.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.patches import Arc, Circle, Ellipse, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle

ROOT = Path(__file__).resolve().parents[2]
FIG = ROOT / "docs" / "figures"
EX = ROOT / "docs" / "architecture" / "examples"

WIDTH_IN = 6.5  # every figure: 1560 px at DPI
DPI = 240
MAX_HEIGHT_PX = 2600  # D1
MIN_PT = 7.0  # 23 px at the source, about 13 px when GitHub shows the figure 880 px wide (D3: at least 20 px)
MAX_LINES = 2  # D2: no text block longer than two lines
MAX_LEGEND = 8  # D2: a longer legend belongs in the Markdown
BODY, HEAD, TITLE = 7.0, 8.0, 9.5

INK, MUTED, LINE, NEUTRAL = "#1F2933", "#52606D", "#334E68", "#9AA5B1"
REPLY = "#7B8794"
# Authority classes. Chosen so every pair stays apart under deuteranopia (CIELAB ΔE > 20, Machado 2009).
OBSERVED, ASSERTED, NOMOS, PLUGIN, APP = "#17406F", "#9F4FC9", "#2E7D32", "#0F7C80", "#A3ABB4"
STAGE = "#E9EDF2"  # a stage, state or fact: no border, no authority
HATCH = "#D3DAE2"  # the light hatch on an item nobody signs
FILL = {"host": "#FBF7EE", "app": "#F3F5F8", "worker": "#EAF2FB", "nomos": "#ECF6EC", "external": "#F7F3FC"}
MONO = "DejaVu Sans Mono"

# key: (colour, tag, legend wording). The same wording in every figure.
PARTY = {
    "observed": (OBSERVED, "O", "Relay host, plugin code: what the call carries"),
    "asserted": (ASSERTED, "A", "asserted: unverified, like the model's intent"),
    "nomos": (NOMOS, "N", "Nomos: Council, Executor, what they sign"),
    "plugin": (PLUGIN, "P", "the plugin's store, and what it signs"),
    "app": (APP, "", "application, operator's host: no Nomos code"),
}
# The overview's legend, in fewer words (the overview has a word budget).
PARTY_SHORT = {
    "observed": "Relay, plugin",
    "asserted": "model",
    "nomos": "Nomos",
    "plugin": "store",
    "app": "no Nomos code",
}
# Outcome classes: one glyph and one colour each, everywhere.
OUTCOMES = {
    "pass": ("→", "#6B7785", "passthrough: not guarded"),
    "refused": ("⊘", "#B42318", "refused by the plugin: nothing ran"),
    "no": ("✕", "#B7791F", "the Council denied, or failed"),
    "ran": ("✓", INK, "dispatched: executed or execution_failed"),
    "unknown": ("?", "#7B341E", "may have run: outcome unknown"),
    "replay": ("↺", MUTED, "replay of the stored outcome"),
}
ARROWS = {
    "call": "every call; disc = connection id",
    "reply": "reply on the same connection",
    "once": "once only (install, worker start)",
}
BLOCKED = "#7B341E"


def ex(name: str):
    return json.loads((EX / f"{name}.json").read_text())


def halo(artist, width=4.0):
    """A white outline, so a line crossing under this one reads as passing, not joining."""
    artist.set_path_effects([pe.Stroke(linewidth=width, foreground="white"), pe.Normal()])
    return artist


def wrap(s: str, n: int) -> str:
    return "\n".join(textwrap.wrap(s, n))


class Canvas:
    """A figure WIDTH_IN wide; one unit is 1/100 of the width on both axes."""

    def __init__(self, h_in: float):
        self.fig = plt.figure(figsize=(WIDTH_IN, h_in))
        self.ax = self.fig.add_axes((0, 0, 1, 1))
        self.ymax = 100 * h_in / WIDTH_IN
        self.ax.set_xlim(0, 100)
        self.ax.set_ylim(0, self.ymax)
        self.ax.axis("off")
        self.boxed: list[tuple[object, tuple[float, float, float, float]]] = []
        self.problems: list[str] = []
        self.n_boxes = 0
        self.panels: list[
            tuple[str, tuple[float, float, float, float]]
        ] = []  # text blocks of at most two rows

    def fit(self, bottom: float) -> None:
        """Cut the figure off at ``bottom`` (keeping one unit = 1/100 of the width)."""
        self.fig.set_size_inches(WIDTH_IN, (self.ymax - bottom) * WIDTH_IN / 100)
        self.ax.set_ylim(bottom, self.ymax)

    # ── primitives ──

    def text(
        self,
        x,
        y,
        s,
        size=BODY,
        color=INK,
        ha="left",
        va="center",
        weight="normal",
        style="normal",
        family=None,
        bg=None,
        z=4,
    ):
        kw = {"bbox": {"fc": bg, "ec": "none", "pad": 0.6}} if bg else {}
        return self.ax.text(
            x,
            y,
            s,
            fontsize=size,
            color=color,
            ha=ha,
            va=va,
            fontweight=weight,
            style=style,
            family=family,
            zorder=z,
            linespacing=1.25,
            **kw,
        )

    def title(self, s, sub=None):
        self.text(1.5, self.ymax - 2.4, s, size=TITLE, weight="bold")
        if sub:
            self.text(1.5, self.ymax - 4.6, sub, color=MUTED, style="italic", va="top")

    def heading(self, y, s):
        self.text(1.5, y, s, weight="bold", color=LINE, size=HEAD)

    def zone(self, x, y, w, h, label, fill, at="top"):
        self.ax.add_patch(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0,rounding_size=1.2",
                fc=fill,
                ec=NEUTRAL,
                lw=0.8,
                ls="--",
                zorder=0,
            )
        )
        self.n_boxes += 1
        if not label:
            return
        if at == "top":
            self.text(x + 1.2, y + h - 0.9, label, color=MUTED, style="italic", va="top", z=1)
        elif at == "bottom-right":
            self.text(x + w - 1.2, y + 0.9, label, color=MUTED, style="italic", ha="right", va="bottom", z=1)
        else:
            self.text(x + 1.2, y + 0.9, label, color=MUTED, style="italic", va="bottom", z=1)

    def box(
        self,
        x,
        y,
        w,
        h,
        name,
        sub="",
        edge=OBSERVED,
        fill="white",
        size=HEAD,
        lw=1.3,
        ls="-",
        z=2,
        hatch=False,
    ):
        """A party (white, its authority's edge), or with ``edge=None`` a stage: shaded, no border.

        ``hatch``: a light diagonal hatch, for an item nobody signs."""
        if edge is None:
            edge, fill = "none", STAGE
        if hatch:
            self.ax.add_patch(
                FancyBboxPatch(
                    (x, y),
                    w,
                    h,
                    boxstyle="round,pad=0,rounding_size=0.9",
                    fc="none",
                    ec="none",
                    hatch="///",
                    hatchcolor=HATCH,
                    lw=0,
                    zorder=z + 0.5,
                )
            )
        self.ax.add_patch(
            FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle="round,pad=0,rounding_size=0.9",
                fc=fill,
                ec=edge,
                lw=lw,
                ls=ls,
                zorder=z,
            )
        )
        self.n_boxes += 1
        lines = name.count("\n") + 1 + (sub.count("\n") + 1 if sub else 0)
        if lines > MAX_LINES:
            self.problems.append(f"box {name!r}: {lines} lines of text")
        bg = "white" if hatch else None
        if sub:
            t = self.text(
                x + w / 2, y + h / 2 + 1.0, name, size=size, weight="bold", ha="center", z=z + 2, bg=bg
            )
            s = self.text(x + w / 2, y + h / 2 - 1.3, sub, color=LINE, ha="center", z=z + 2, bg=bg)
            self.boxed += [(t, (x, y, w, h)), (s, (x, y, w, h))]
        else:
            t = self.text(x + w / 2, y + h / 2, name, size=size, weight="bold", ha="center", z=z + 2, bg=bg)
            self.boxed.append((t, (x, y, w, h)))

    def store(self, x, y, w, h, name, sub=""):
        """The evidence store: the one cylinder in every figure."""
        e = 2.4
        for yy in (y + h - e / 2, y):
            self.ax.add_patch(
                Ellipse((x + w / 2, yy), w, e, fc="white", ec=PLUGIN, lw=1.3, zorder=2 if yy == y else 3)
            )
        self.ax.add_patch(Rectangle((x, y), w, h - e / 2, fc="white", ec="none", zorder=2.5))
        self.ax.plot([x, x], [y, y + h - e / 2], color=PLUGIN, lw=1.3, zorder=3)
        self.ax.plot([x + w, x + w], [y, y + h - e / 2], color=PLUGIN, lw=1.3, zorder=3)
        self.ax.add_patch(Arc((x + w / 2, y), w, e, theta1=180, theta2=360, ec=PLUGIN, lw=1.3, zorder=3))
        self.n_boxes += 1
        mid = y + (h - e / 2) / 2
        if sub:
            t = self.text(x + w / 2, mid + 1.0, name, size=HEAD, weight="bold", ha="center")
            s = self.text(x + w / 2, mid - 1.3, sub, color=LINE, ha="center")
            self.boxed += [(t, (x, y - e / 2, w, h + e)), (s, (x, y - e / 2, w, h + e))]
        else:
            t = self.text(x + w / 2, mid, name, size=HEAD, weight="bold", ha="center")
            self.boxed.append((t, (x, y - e / 2, w, h + e)))

    def arrow(self, a, b, once=False, color=LINE, rad=0.0, lw=1.1, head="-|>", z=3, reply=False, ring=False):
        if reply:
            head, lw, color = "->", 0.9, REPLY
        p = FancyArrowPatch(
            a,
            b,
            arrowstyle=head,
            mutation_scale=9,
            lw=lw,
            color=color,
            zorder=z,
            connectionstyle=f"arc3,rad={rad}",
            ls=(0, (4, 2.5)) if once else "-",
            shrinkA=0,
            shrinkB=0,
        )
        self.ax.add_patch(p)
        if ring:
            halo(p)
        return p

    def route(self, points, once=False, color=LINE, lw=1.1, ring=False, reply=False):
        """An arrow through right-angled corners: a line through all but the last point, then the arrow."""
        xs, ys = zip(*points[:-1], strict=True)
        (line,) = self.ax.plot(
            xs,
            ys,
            color=REPLY if reply else color,
            lw=0.9 if reply else lw,
            zorder=3,
            ls=(0, (4, 2.5)) if once else "-",
            solid_capstyle="butt",
        )
        if ring:
            halo(line)
        return self.arrow(points[-2], points[-1], once=once, color=color, lw=lw, ring=ring, reply=reply)

    def badge(self, x, y, cid, once=False):
        """A connection id on its arrow: a dark disc for per-call links, an outlined one for once-only links."""
        self.ax.add_patch(Circle((x, y), 1.9, fc="white" if once else INK, ec=INK, lw=1.0, zorder=6))
        self.text(
            x, y, cid, size=BODY, color=INK if once else "white", ha="center", weight="bold", z=7
        ).set_gid("on-arrow")

    def mark(self, x, y, kind, r=1.5):
        glyph, color, _ = OUTCOMES[kind]
        self.ax.add_patch(Circle((x, y), r, fc=color, ec="white", lw=0.8, zorder=6))
        self.text(x, y - 0.1, glyph, size=BODY + 0.5, color="white", ha="center", weight="bold", z=7).set_gid(
            "on-arrow"
        )

    def diamond(self, x, y, r=1.6, color=INK):
        self.ax.add_patch(
            Polygon(
                [(x - r, y), (x, y + r), (x + r, y), (x, y - r)],
                closed=True,
                fc="white",
                ec=color,
                lw=1.2,
                zorder=6,
            )
        )

    def tag(self, x, y, key):
        """The one-letter authority tag (O, A, N, P) that repeats a colour in shape-independent form."""
        color, letter, _ = PARTY[key]
        self.ax.add_patch(
            FancyBboxPatch(
                (x, y - 0.95),
                2.2,
                1.9,
                boxstyle="round,pad=0,rounding_size=0.35",
                fc=color,
                ec=color,
                zorder=6,
            )
        )
        if letter:
            self.text(x + 1.1, y - 0.05, letter, color="white", ha="center", weight="bold", z=7).set_gid(
                "on-arrow"
            )

    def pill(self, x, y, s, edge=INK, fill="white", color=INK, family=None, weight="normal", ls="-"):
        """A short label in a rounded outline (``edge=None``: a shaded fact, no border). Returns its right edge."""
        if edge is None:
            edge, fill = "none", STAGE
        w = pill_width(s, family)
        self.ax.add_patch(
            FancyBboxPatch(
                (x, y - 1.2),
                w,
                2.4,
                boxstyle="round,pad=0,rounding_size=1.0",
                fc=fill,
                ec=edge,
                lw=1.0,
                ls=ls,
                zorder=5,
            )
        )
        t = self.text(x + w / 2, y, s, color=color, ha="center", family=family, weight=weight, z=6)
        self.boxed.append((t, (x, y - 1.2, w, 2.4)))
        return x + w

    def card(self, x, top, w, title, body, color, h=None):
        """A titled card: a coloured title bar, then at most two lines. Returns its bottom."""
        h = h or 3.2 + 2.3 * (body.count("\n") + 1) + 2.0
        self.ax.add_patch(
            FancyBboxPatch(
                (x, top - h),
                w,
                h,
                boxstyle="round,pad=0,rounding_size=0.7",
                fc="white",
                ec=color,
                lw=1.2,
                zorder=2,
            )
        )
        self.ax.add_patch(
            FancyBboxPatch(
                (x, top - 3.2), w, 3.2, boxstyle="round,pad=0,rounding_size=0.7", fc=color, ec=color, zorder=2
            )
        )
        self.n_boxes += 1
        t = self.text(x + 1.0, top - 1.6, title, weight="bold", color="white")
        b = self.text(x + 1.0, top - 4.4, body, va="top")
        self.boxed += [(t, (x, top - 3.2, w, 3.2)), (b, (x, top - h, w, h))]
        self.panels.append((title, (x, top - h, w, h - 3.2)))
        return top - h

    # ── the legend: only the encodings a figure uses, always in the same words ──

    def legend(self, y, parties=(), arrows=(), outcomes=(), extra=(), cols=2, short=False, tags=False):
        """Entries in ``cols`` columns (under the heading "Legend" unless ``short``); returns the y under it.

        A party is shown as a swatch shaped like the boxes it explains, or with ``tags`` as the one-letter tag the
        figure puts on its fields."""
        entries = (
            [("party", k) for k in parties]
            + [("arrow", k) for k in arrows]
            + [("mark", k) for k in outcomes]
            + [("extra", e) for e in extra]
        )
        if len(entries) > MAX_LEGEND:
            self.problems.append(f"legend has {len(entries)} entries (at most {MAX_LEGEND})")
        if not short:
            self.text(1.5, y, "Legend", weight="bold", color=LINE)
            y -= 2.9
        width = 98 / cols
        for i, (kind, key) in enumerate(entries):
            x, ry = 1.5 + width * (i % cols), y - 3.0 * (i // cols)
            if kind == "party":
                if not tags:  # a swatch shaped like the boxes it explains
                    self.ax.add_patch(
                        FancyBboxPatch(
                            (x, ry - 0.95),
                            2.2,
                            1.9,
                            boxstyle="round,pad=0,rounding_size=0.35",
                            fc="white",
                            ec=PARTY[key][0],
                            lw=1.6,
                            zorder=5,
                        )
                    )
                elif PARTY[key][1]:
                    self.tag(x, ry, key)
                else:
                    self.ax.add_patch(Rectangle((x, ry - 0.95), 2.2, 1.9, fc=APP, ec=APP, zorder=5))
                label = PARTY_SHORT[key] if short else PARTY[key][2]
            elif kind == "arrow":
                if key == "call":
                    self.arrow((x, ry), (x + 7, ry))
                    self.badge(x + 3.0, ry, "C")
                elif key == "reply":
                    self.arrow((x, ry), (x + 7, ry), reply=True)
                else:
                    self.arrow((x, ry), (x + 7, ry), once=True)
                    self.badge(x + 3.0, ry, "C", once=True)
                label = key if short else ARROWS[key]
            elif kind == "mark":
                self.mark(x + 1.2, ry, key, r=1.2)
                label = OUTCOMES[key][2]
            else:
                draw, label = key
                draw(x, ry)
            self.text(x + (8.2 if kind == "arrow" else 3.2), ry, label)
        return y - 3.0 * ((len(entries) + cols - 1) // cols)

    # ── output ──

    def save(self, name: str) -> Path:
        FIG.mkdir(parents=True, exist_ok=True)
        path = FIG / name
        self._check(name)
        self.fig.savefig(path, dpi=DPI)
        plt.close(self.fig)
        return path

    def _check(self, name: str) -> None:
        r = self.fig.canvas.get_renderer()
        texts = [t for t in self.ax.texts if t.get_text().strip()]
        problems = list(self.problems)
        width, height = (round(v * DPI) for v in self.fig.get_size_inches())
        if height > MAX_HEIGHT_PX:
            problems.append(f"{width} x {height} px: taller than {MAX_HEIGHT_PX} px")
        problems += [f"{t.get_text()!r} is {t.get_fontsize()} pt" for t in texts if t.get_fontsize() < MIN_PT]
        problems += [
            f"{t.get_text()!r} has more than {MAX_LINES} lines"
            for t in texts
            if t.get_text().count("\n") >= MAX_LINES
        ]
        boxes = {id(t): t.get_window_extent(r) for t in texts}
        to_px = self.ax.transData.transform
        for t, (x, y, w, h) in self.boxed:
            (x0, y0), (x1, y1) = to_px((x, y)), to_px((x + w, y + h))
            if id(t) not in boxes:  # an empty label
                continue
            b = boxes[id(t)]
            if b.x0 < x0 + 1 or b.x1 > x1 - 1 or b.y0 < y0 or b.y1 > y1:
                problems.append(f"{t.get_text()!r} overflows its box")
        # A list drawn as separate stacked texts is still one block: count the rows inside each card or panel.
        for label, (x, y, w, h) in self.panels:
            (x0, y0), (x1, y1) = to_px((x, y)), to_px((x + w, y + h))
            ys = []
            for t in texts:
                b = boxes[id(t)]
                cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
                if not (x0 < cx < x1 and y0 < cy < y1) or t.get_gid() == "on-arrow":
                    continue
                n = t.get_text().count("\n") + 1
                ys += [b.y1 - (i + 0.5) * b.height / n for i in range(n)]
            ys.sort()
            rows = sum(
                1 for i, v in enumerate(ys) if i == 0 or v - ys[i - 1] > 12
            )  # texts on one baseline: one row
            if rows > MAX_LINES:
                problems.append(f"panel {label!r} holds {rows} rows of text")
        for t in texts:
            b = boxes[id(t)]
            if b.x0 < 0 or b.x1 > self.fig.bbox.width:
                problems.append(f"{t.get_text()!r} runs off the figure")
        for i, a in enumerate(texts):
            for b in texts[i + 1 :]:
                ba, bb = boxes[id(a)].shrunk(0.98, 0.9), boxes[id(b)].shrunk(0.98, 0.9)
                if ba.overlaps(bb):
                    problems.append(f"{a.get_text()!r} overlaps {b.get_text()!r}")
        if problems:
            report = f"{name}:\n  " + "\n  ".join(problems)
            if os.environ.get("FIGURES_DRAFT"):  # draw anyway, to look at the problem
                print(report, file=sys.stderr)
            else:
                raise SystemExit(report)
        for p in self.ax.patches:
            if not isinstance(p, FancyArrowPatch):
                continue
            paths, _ = p._get_path_in_displaycoord()
            points = []
            for path in paths:
                for poly in path.to_polygons(closed_only=False):
                    for (x0, y0), (x1, y1) in zip(poly[:-1], poly[1:], strict=False):
                        points += [(x0 + (x1 - x0) * k / 40, y0 + (y1 - y0) * k / 40) for k in range(41)]
            for t in texts:
                if t.get_gid() == "on-arrow":  # a connection id or a mark sits on its arrow by design
                    continue
                b = boxes[id(t)].shrunk(0.96, 0.8)
                if any(b.x0 < x < b.x1 and b.y0 < y < b.y1 for x, y in points):
                    print(f"  warning {name}: an arrow crosses {t.get_text()!r}", file=sys.stderr)


def pill_width(s: str, family=None) -> float:
    return (0.86 if family == MONO else 0.79) * len(s) + 0.25 * sum(ch in "_mwMW" for ch in s) + 2.0


def overview_words(c: Canvas) -> int:
    """Words in the figure's text, not counting connection ids, marks and step numbers."""
    words = [
        w for t in c.ax.texts if t.get_gid() != "on-arrow" for w in t.get_text().replace("·", " ").split()
    ]
    return sum(not w.isdigit() for w in words)


# ── 0. the whole system on one screen ─────────────────────────────────────


def overview() -> Path:
    c = Canvas(4.55)
    T = c.ymax
    c.title("Who does what")
    c.zone(1.5, T - 29, 57, 23, "Application process", FILL["app"])
    c.zone(1.5, T - 64.5, 57, 31, "Plugin worker", FILL["worker"], at="bottom-right")
    c.zone(70, T - 64.5, 28.5, 36, "Nomos deployment", FILL["nomos"])
    c.box(4, T - 15.5, 15, 6.5, "Application", edge=APP)
    c.box(4, T - 26.5, 15, 7, "App's tool", edge=APP)
    c.box(26, T - 26.5, 31, 17, "Relay host", edge=OBSERVED)
    c.box(72, T - 16, 25, 7, "Model provider", edge=ASSERTED)
    c.box(3.5, T - 48, 21, 8, "H1", "intent, turn token", edge=OBSERVED)
    c.box(30, T - 48, 27, 8, "H3", "verifies writ, dispatches", edge=OBSERVED)
    c.store(5, T - 62, 28, 7.5, "Evidence store")
    c.box(72, T - 47, 25, 9, "Council", "decides", edge=NOMOS)
    c.box(72, T - 60, 25, 7, "Executor", "runs commands", edge=NOMOS)
    # C1, C2
    c.arrow((19, T - 12.25), (26, T - 12.25))
    c.badge(22.5, T - 12.25, "C1")
    c.arrow((57, T - 12.5), (72, T - 12.5))
    c.badge(64.5, T - 12.5, "C2")
    c.text(64.5, T - 9.4, "edited request", ha="center")
    c.arrow((26, T - 23), (19, T - 23))
    c.text(22.5, T - 20.6, "runs", ha="center")
    # 1: every model request reaches H1; 2: every tool call reaches H3; 6: the result goes back
    c.arrow((29, T - 26.5), (16, T - 40))
    c.badge(22.5, T - 33.25, "C3")
    c.text(20.2, T - 31, "1 request", ha="right")
    c.arrow((34, T - 26.5), (34, T - 40))
    c.badge(34, T - 33.25, "C3")
    c.text(32.4, T - 37.2, "2 tool call", ha="right")
    c.arrow((39.5, T - 40), (39.5, T - 26.5), reply=True)
    c.badge(39.5, T - 33.25, "C3")
    c.text(41.2, T - 37.2, "6 result, or why\nnothing ran")
    # 3: the Council decides; 4: an approved call runs, as a local tool through Relay or on the Executor
    c.arrow((57, T - 40.5), (72, T - 40.5))
    c.badge(64.25, T - 40.5, "C4")
    c.text(64.25, T - 37.6, "3 ask", ha="center")
    c.arrow((72, T - 45.5), (57, T - 45.5), reply=True)
    c.badge(64.25, T - 45.5, "C4")
    c.text(64.25, T - 48.4, "signed writ,\nor reasons", ha="center", va="top")
    c.arrow((55.5, T - 40), (55.5, T - 26.5))
    c.badge(55.5, T - 33.25, "C3")
    c.text(57.3, T - 30.6, "4 local")
    c.route([(51, T - 48), (51, T - 57), (72, T - 57)])
    c.badge(64.25, T - 57, "C5")
    c.text(64.25, T - 59.9, "4 command", ha="center")
    # 1: H1 opens a turn; 5: H3 stores the outcome before it returns it (6)
    c.arrow((12, T - 48), (14, T - 54.6))
    c.badge(13, T - 51.3, "C6")
    c.text(10.6, T - 51.3, "1 turn", ha="right")
    c.arrow((36, T - 48), (29, T - 54.6))
    c.badge(32.5, T - 51.3, "C6")
    c.text(34.9, T - 51.3, "5 stored")
    end = c.legend(
        T - 66.6,
        parties=("observed", "asserted", "nomos", "plugin", "app"),
        arrows=("reply",),
        cols=6,
        short=True,
    )
    c.fit(end - 0.9)
    words, (w, h) = overview_words(c), (round(v * DPI) for v in c.fig.get_size_inches())
    if words > 60 or c.n_boxes > 12 or w > 1560 or h > 1100:
        c.problems.append(f"overview: {words} words, {c.n_boxes} boxes, {w} x {h} px")
    return c.save("overview.png")


# ── 1. what is connected to the plugin ────────────────────────────────────


def connections() -> Path:
    c = Canvas(9.0)
    T = c.ymax
    c.title(
        "What the plugin is connected to, and over which connection (C1–C8)",
        "Arrows start at the initiator. What crosses each connection: the table under this figure.",
    )
    # places
    c.zone(1, T - 103, 70, 95, "Operator's host", FILL["host"], at="bottom")
    c.zone(3, T - 33, 66, 23, "Application process: no Nomos code", FILL["app"])
    c.zone(3, T - 87, 66, 49, "Nomos plugin worker: a separate process", FILL["worker"], at="bottom-right")
    c.zone(73, T - 87, 26, 49, "Nomos deployment", FILL["nomos"])
    # parties
    c.box(5, T - 21, 19, 7, "Application", "agent loop", edge=APP)
    c.box(5, T - 31, 19, 7, "App's tool", "a local tool", edge=APP)
    c.box(42, T - 31, 25, 18, "Relay host", "in the app's process", edge=OBSERVED)
    c.box(75, T - 21, 23, 7, "Model provider", "external", edge=ASSERTED)
    c.box(25, T - 55, 13, 7, "H1", "request hook", edge=OBSERVED)
    c.box(51, T - 55, 16, 7, "H3", "tool-call hook", edge=OBSERVED)
    c.box(44, T - 66, 23, 7, "Writ handler", "verify · claim · dispatch", edge=OBSERVED)
    c.box(5, T - 66, 17, 7, "Config loader", "values · settings", edge=OBSERVED)
    c.store(24, T - 82, 43, 9, "Evidence store", "SQLite :memory:, inside the worker")
    c.box(75, T - 55, 23, 7, "Council", "signs writs", edge=NOMOS)
    c.box(75, T - 72, 23, 7, "Executor", "runs commands, signs results", edge=NOMOS, size=HEAD)
    c.box(5, T - 99, 17, 7, "Secret files", "tokens · signing key", edge=APP)
    c.box(27, T - 99, 14, 7, "Relay CLI", "plugins add", edge=APP)
    c.box(48, T - 99, 19, 7, "Worker venv", "the bundle", edge=APP)

    # C1: application -> Relay host
    c.arrow((24, T - 17.5), (42, T - 17.5))
    c.badge(33, T - 17.5, "C1")
    c.text(33, T - 14.8, "llm.execute", ha="center")
    c.text(33, T - 20.2, "tools.execute", ha="center")
    # Relay host -> the app's tool: a plain call inside the application process (no connection id)
    c.arrow((42, T - 27.5), (24, T - 27.5))
    c.text(33, T - 25.6, "runs it in-process", ha="center")
    # C2: Relay host -> model provider
    c.arrow((67, T - 17.5), (75, T - 17.5))
    c.badge(71, T - 17.5, "C2")
    c.text(75.5, T - 23, "→ the edited request\n← the reply (a tool call)", va="top")
    # C3: Relay starts the worker, once
    c.arrow((63.5, T - 31), (63.5, T - 38), once=True)
    c.badge(63.5, T - 34.5, "C3", once=True)
    c.text(65.7, T - 34.5, "starts", color=MUTED, style="italic")
    # C3: Relay host -> H1 and H3
    c.arrow((43, T - 31), (33, T - 48))
    c.badge(38.3, T - 40, "C3")
    c.text(36.1, T - 37.4, "model\nrequest", ha="right")
    c.arrow((59, T - 31), (59, T - 48))
    c.badge(59, T - 42.5, "C3")
    c.text(57.5, T - 45.6, "tool call", ha="right")
    # C3: the writ handler's continuation back through Relay (local tools after approval)
    c.arrow((47, T - 59), (47, T - 31), ring=True, z=4)
    c.badge(47, T - 40.5, "C3")
    c.text(48.6, T - 37, "next_: the\nlocal tool")
    # H3 -> writ handler: a function call inside the worker (no connection)
    c.arrow((59, T - 55), (59, T - 59), lw=0.9)
    c.text(60.4, T - 57, "writ", color=MUTED, style="italic")
    # C4: H3 -> Council
    c.arrow((67, T - 51.5), (75, T - 51.5))
    c.badge(71, T - 51.5, "C4")
    c.text(75.5, T - 57, "→ council_authorize\n← decision (+ writ)", va="top")
    # C5: writ handler -> Executor
    c.arrow((67, T - 62.5), (75, T - 66))
    c.badge(71, T - 64.25, "C5")
    c.text(75.5, T - 74, "→ POST /execute {writ}\n← signed result", va="top")
    c.text(
        75.5, T - 85.5, "No Council–Executor link:\nthe worker carries the writ.", va="bottom", color=MUTED
    )
    # C6: H1, H3 and the writ handler -> the store (H3's line passes under the next_ arrow)
    c.arrow((31.5, T - 55), (31.5, T - 72))
    c.badge(31.5, T - 63.5, "C6")
    c.route([(51, T - 53), (41, T - 53), (41, T - 72)])
    c.badge(41, T - 63.5, "C6")
    c.arrow((58, T - 66), (58, T - 72))
    c.badge(58, T - 69, "C6")
    # C7: config loader -> secret files, once at start
    c.arrow((13.5, T - 66), (13.5, T - 92), once=True)
    c.badge(13.5, T - 89, "C7", once=True)
    c.text(16, T - 89, "at worker start", color=MUTED, style="italic")
    # C8: Relay CLI -> worker venv, once at install
    c.arrow((41, T - 95.5), (48, T - 95.5), once=True)
    c.badge(44.5, T - 95.5, "C8", once=True)
    c.text(44.5, T - 101, "at install", ha="center", color=MUTED, style="italic")
    end = c.legend(
        T - 107, parties=("observed", "asserted", "nomos", "plugin", "app"), arrows=("call", "once")
    )
    c.fit(end - 0.8)
    return c.save("connections.png")


# ── 2. the two hooks, in order ────────────────────────────────────────────


class Sequence:
    """A sequence diagram: lanes left to right, time running down, numbered messages and the checks that can end
    the call early (a mark on the H3 lane, with its reason)."""

    def __init__(self, c: Canvas, lanes: dict, top: float):
        self.c, self.lanes, self.y = c, lanes, top
        self.X = {k: v[0] for k, v in lanes.items()}
        self.head = top - 3.4
        self.y = top - 9.5

    def msg(self, a, b, label, cid=None, reply=False, lines=None, at=0.5):
        """A message from lane ``a`` to lane ``b``; ``at`` places its connection id along it (0 at ``a``)."""
        c = self.c
        n = lines or label.count("\n") + 1
        self.y -= 2.3 * n + 0.6
        xa, xb = self.X[a], self.X[b]
        c.arrow((xa, self.y), (xb, self.y), reply=reply)
        if cid:
            c.badge(xa + (xb - xa) * at, self.y, cid)
        c.text(min(xa, xb) + 0.8, self.y + 1.9, label, va="bottom", bg="white")
        self.y -= 2.6

    def note(self, lane, label):
        c = self.c
        n = label.count("\n") + 1
        h = 2.3 * n + 0.4
        self.y -= h / 2 + 0.6
        x = self.X[lane]
        c.ax.add_patch(Rectangle((x - 0.6, self.y - h / 2), 1.2, h, fc="white", ec=LINE, lw=0.9, zorder=3))
        c.text(x + 1.6, self.y, label, bg="white")
        self.y -= h / 2 + 1.8

    def end(self, lane, kind, label):
        """A check that ends the call: the outcome's mark on the lane, and why."""
        c = self.c
        n = label.count("\n") + 1
        self.y -= 1.15 * n + 1.0
        x = self.X[lane]
        c.mark(x, self.y, kind)
        c.text(x + 2.4, self.y, label, bg="white")
        self.y -= 1.15 * n + 2.0

    def phase(self, text):
        c = self.c
        self.y -= 1.0
        c.ax.plot([1.5, 98.5], [self.y, self.y], color=LINE, lw=0.8, zorder=1)
        c.text(50, self.y, text, weight="bold", color=LINE, ha="center", bg="white")
        self.y -= 2.4

    def close(self, zones):
        """Draw the places behind, the lane heads and the lifelines; returns the bottom."""
        c, bottom = self.c, self.y + 0.5
        for x0, x1, label, fill in zones:
            c.ax.add_patch(
                FancyBboxPatch(
                    (x0, bottom),
                    x1 - x0,
                    self.head + 6.2 - bottom,
                    boxstyle="round,pad=0,rounding_size=1",
                    fc=fill,
                    ec=NEUTRAL,
                    lw=0.8,
                    ls="--",
                    zorder=0,
                )
            )
            c.text((x0 + x1) / 2, self.head + 4.9, label, color=MUTED, style="italic", ha="center")
        for x, name, edge in self.lanes.values():
            c.box(x - 6.5, self.head - 3.2, 13, 6.4, name, edge=edge, size=BODY, z=4)
        for x, _, _ in self.lanes.values():
            c.ax.plot([x, x], [bottom + 0.5, self.head - 3.2], color=NEUTRAL, lw=0.9, zorder=1)
        return bottom


def h1() -> Path:
    c = Canvas(8.0)
    top = c.ymax
    c.title(
        "What H1 does to every model request",
        "Time runs down; the numbers give the order. H1 edits the request; only the Relay host talks to the model.",
    )
    lanes = {
        "app": (8.5, "Application", APP),
        "relay": (26.5, "Relay host", OBSERVED),
        "model": (45, "Model\nprovider", ASSERTED),
        "h1": (62, "H1", OBSERVED),
        "store": (91, "Evidence\nstore", PLUGIN),
    }
    q = Sequence(c, lanes, top - 11)
    q.msg("app", "relay", "1  llm.execute(request)", "C1")
    q.msg("relay", "h1", "2  model name, request", "C3", at=0.88)
    q.note("h1", "3  adds the nomos field to guarded tools:\n    the same bytes on every request")
    q.msg(
        "h1",
        "store",
        "4  opens a turn: a NEW token, in the one\n    conversation it shows, else a new one",
        "C6",
    )
    q.msg(
        "h1",
        "store",
        "5  catalog (signing key only): ids these\n    messages show, in citable conversations",
        "C6",
    )
    q.msg(
        "h1",
        "relay",
        "6  edited request + one Nomos message:\n    the turn token, the catalog (never output)",
        "C3",
        True,
        at=0.12,
    )
    q.msg("relay", "model", "7  the edited request", "C2")
    q.msg("model", "relay", "8  a tool call; nomos.turn = the token", "C2", True)
    q.msg("relay", "app", "9  the reply", "C1", True)
    bottom = q.close(
        [
            (1, 35.5, "application process", FILL["app"]),
            (36.2, 53.8, "external", FILL["external"]),
            (54.5, 99.4, "Nomos plugin worker (separate process)", FILL["worker"]),
        ]
    )
    c.text(
        1.5,
        bottom - 3,
        "The next request gets a new token again; the nomos field H1 adds (3) is the same every time.",
        color=LINE,
    )
    end = c.legend(bottom - 7.5, parties=("app", "observed", "asserted", "plugin"), arrows=("call", "reply"))
    c.fit(end - 0.8)
    return c.save("h1.png")


def _alt(c: Canvas, x0, x1, top, mid, bottom, first, second):
    """A UML alt frame: exactly one of its two parts runs."""
    c.ax.add_patch(Rectangle((x0, bottom), x1 - x0, top - bottom, fc="none", ec=LINE, lw=1.0, zorder=2))
    c.ax.add_patch(
        Polygon(
            [(x0, top), (x0 + 13, top), (x0 + 13, top - 1.6), (x0 + 11.8, top - 2.8), (x0, top - 2.8)],
            closed=True,
            fc="white",
            ec=LINE,
            lw=1.0,
            zorder=2,
        )
    )
    c.text(x0 + 0.8, top - 1.4, "alt: one of", weight="bold", color=LINE)
    c.text(x0 + 14, top - 1.4, first, weight="bold", color=LINE)
    c.ax.plot([x0, x1], [mid, mid], color=LINE, lw=0.9, ls=(0, (4, 2.5)), zorder=2)
    c.text(x0 + 0.8, mid - 1.4, second, weight="bold", color=LINE)


def h3_authorize() -> Path:
    c = Canvas(10.8)
    top = c.ymax
    c.title(
        "What happens to a guarded tool call before it may run",
        "Time runs down; the numbers continue H1's. A mark on the H3 line ends the call there, for the reason given;\n"
        "every ending goes back to the application as an ordinary tool result (C3, then C1).",
    )
    lanes = {
        "app": (8, "Application", APP),
        "relay": (24, "Relay host", OBSERVED),
        "h3": (44, "H3", OBSERVED),
        "store": (68, "Evidence\nstore", PLUGIN),
        "council": (91, "Council", NOMOS),
    }
    q = Sequence(c, lanes, top - 13)
    q.msg("app", "relay", "10  tools.execute(name, args, tool_call_id)", "C1")
    q.msg("relay", "h3", "11  name, args with nomos, tool_call_id", "C3")
    q.end(
        "h3",
        "refused",
        "args not a bounded JSON object: invalid_arguments; nomos missing\n"
        "or malformed: missing_intent (the result shows the field to send)",
    )
    q.msg("h3", "store", "12  nomos.turn → its conversation, and\n      that turn's user request", "C6")
    q.end(
        "h3",
        "refused",
        "turn unknown or expired: unknown_turn\n(the result says: call again with the latest turn)",
    )
    q.end(
        "h3",
        "refused",
        "a call of this conversation is still running: session_busy",
    )
    q.end("h3", "refused", "tool policy, trust snapshot or Council key fails")
    q.phase("no journal row above this line: an ending here is returned at once")
    q.msg(
        "h3",
        "store",
        "13  T1: replay, conflict, blocked and quota checks;\n      journal row 'authorizing'",
        "C6",
    )
    q.end(
        "h3",
        "replay",
        "same tool_call_id and args in this conversation: its stored outcome",
    )
    q.end(
        "h3",
        "refused",
        "conflict, blocked conversation, over quota:\nthe store refuses: no journal row, nothing stored",
    )
    q.phase("the journal row exists: every ending below is journaled at T3 (step 23) first")
    q.msg(
        "h3",
        "store",
        "14  the cited records, only from conversations\n      this turn may cite; the denial count",
        "C6",
    )
    q.end(
        "h3",
        "refused",
        "a citation fails, a later request showed the turn (with\ntool_definitions: unknown_turn), or the denial cap: no Council",
    )
    q.msg("h3", "store", "15  cited records → 'submitted_unknown'", "C6")
    q.msg("h3", "council", "16  council_authorize(fields)", "C4", at=0.72)
    q.msg("council", "h3", "17  the decision; a signed writ if approved", "C4", True, at=0.28)
    q.end(
        "h3",
        "no",
        "denied (count + 1) or council_error (count kept):\nnothing ran; journaled at T3",
    )
    q.msg(
        "h3",
        "relay",
        "the model's tool result: the Council's whole answer\n"
        "but the writ (reasons, votes, missing permissions)",
        "C3",
        True,
        at=0.5,
    )
    bottom = q.close(
        [
            (1, 32.5, "application process: no Nomos code", FILL["app"]),
            (33.5, 80, "Nomos plugin worker", FILL["worker"]),
            (81, 99.4, "Nomos deployment", FILL["nomos"]),
        ]
    )
    c.text(
        1.5, bottom - 3, "Approved: the writ goes on to the writ handler (next figure, step 18).", color=LINE
    )
    end = c.legend(
        bottom - 7.5,
        parties=("observed", "plugin", "nomos"),
        arrows=("call", "reply"),
        outcomes=("refused", "no", "replay"),
    )
    c.fit(end - 0.8)
    return c.save("h3-authorize.png")


def h3_dispatch() -> Path:
    c = Canvas(10.5)
    top = c.ymax
    c.title(
        "What happens once the Council approves a call",
        "Time runs down; the numbers continue the previous figure's. The outcome is journaled before anyone sees it.",
    )
    lanes = {
        "app": (7.5, "Application", APP),
        "tool": (21, "App's tool", APP),
        "relay": (35, "Relay host", OBSERVED),
        "h3": (53, "H3 + writ\nhandler", OBSERVED),
        "store": (72, "Evidence\nstore", PLUGIN),
        "executor": (91, "Executor", NOMOS),
    }
    q = Sequence(c, lanes, top - 11)
    q.note("h3", "18  verifies the writ: the Council's\n      key, this exact call, fresh")
    q.end("h3", "refused", "a check fails: refused, journaled at T3;\nthe denial count is not reset")
    q.msg("h3", "store", "19  T2: claim the nonce,\n      then row 'dispatching'", "C6")
    q.end("h3", "refused", "nonce already claimed: writ_replayed\n(any tool); journaled at T3")
    frame_top = q.y - 0.6
    q.y -= 2.6
    q.msg("h3", "executor", "20  POST /execute {writ}", "C5")
    q.msg("executor", "h3", "21  ExecutionResult, Executor-signed", "C5", True)
    q.end(
        "h3",
        "ran",
        "signature verified: executed, or execution_failed",
    )
    q.end(
        "h3",
        "unknown",
        "execution_unknown (no, oversized or unreadable\nanswer), evidence_invalid: may have run; not retried",
    )
    divider = q.y - 0.2
    q.y -= 2.4
    q.msg("h3", "relay", "20  next_(args without nomos)", "C3")
    q.msg("relay", "tool", "runs it in-process")
    q.msg("tool", "relay", "its output", reply=True)
    q.msg("relay", "h3", "21  the tool's output", "C3", True)
    q.end(
        "h3",
        "ran",
        "executed, or execution_failed (the tool raised);\nwith a signing_key, the plugin certifies it",
    )
    frame_bottom = q.y + 0.6
    _alt(c, 12.5, 99, frame_top, divider, frame_bottom, "[nomos_http tool]", "[local tool]")
    q.y -= 1.0
    q.msg("h3", "store", "22  the writ was valid: cited records\n      'accepted', denial count → 0", "C6")
    q.msg(
        "h3",
        "store",
        "23  T3: row 'complete' + the record, at once;\n      after a ?, the conversation is blocked",
        "C6",
    )
    q.msg(
        "h3",
        "relay",
        "24  the tool result + a note only the host\n      sees (the Council's votes, timings, trace)",
        "C3",
        True,
    )
    q.msg("relay", "app", "25  the tool result", "C1", True)
    bottom = q.close(
        [
            (1, 42, "application process: no Nomos code", FILL["app"]),
            (43, 81, "Nomos plugin worker", FILL["worker"]),
            (82, 99.4, "Nomos deployment", FILL["nomos"]),
        ]
    )
    c.text(
        1.5,
        bottom - 3,
        "26  The model reads the result in its next request: back to step 1.",
        color=LINE,
    )
    end = c.legend(
        bottom - 7.5,
        parties=("observed", "plugin", "nomos"),
        arrows=("call", "reply"),
        outcomes=("refused", "ran", "unknown"),
    )
    c.fit(end - 0.8)
    return c.save("h3-dispatch.png")


# ── 3. every ending ───────────────────────────────────────────────────────


def outcomes() -> Path:
    c = Canvas(8.0)
    top = c.ymax
    c.title(
        "What each ending of a guarded call leaves behind",
        "Top to bottom in call order (the sequence figures' steps). Status codes and counts: the table under it.",
    )
    rows = [
        ("11", "pass", "not guarded: the tool runs as called", ["not kept"]),
        (
            "11–12",
            "refused",
            "bad arguments, intent or turn: the result says the fix",
            ["record, no conversation"],
        ),
        ("12", "refused", "another call of this conversation is running", ["not kept"]),
        ("12", "refused", "the tool policy or the trust snapshot fails", ["record"]),
        ("13", "replay", "the same call again: its stored outcome", ["nothing new"]),
        ("13", "refused", "the store refuses: conflict, blocked, quota", ["not kept"]),
        None,
        ("14", "refused", "a citation fails, the turn was superseded, or the denial cap", ["record"]),
        ("17", "no", "the Council denies, or fails", ["record, certified"]),
        ("18–19", "refused", "the writ fails a check, or was used", ["record"]),
        ("20–21", "ran", "nomos_http: the Executor-signed result verified", ["record, certified"]),
        ("20–21", "ran", "local: the tool's output, certified by the plugin", ["record, certified"]),
        ("20–21", "unknown", "may have run", ["record", "blocks"]),
    ]
    y = top - 11
    c.text(99, y, "leaves behind", ha="right", color=MUTED, style="italic")
    y -= 3.4
    for row in rows:
        if row is None:
            y -= 0.6
            c.ax.plot([1.5, 98.5], [y, y], color=LINE, lw=1.2, zorder=1)
            c.text(
                50,
                y,
                "above: no journal row is written · below: past T1, journaled at T3 before it is returned",
                color=LINE,
                weight="bold",
                ha="center",
                bg="white",
            )
            y -= 3.6
            continue
        step, kind, text, left = row
        c.box(1.5, y - 1.7, 11, 3.4, step, edge=None, size=BODY)
        c.mark(16.8, y, kind)
        c.text(19.2, y, text)
        px = 99
        for label in reversed(left):
            px -= pill_width(label)
            if label == "blocks":
                c.pill(px, y, label, edge=BLOCKED, fill=BLOCKED, color="white", weight="bold")
            elif label.startswith("record"):
                c.pill(px, y, label, edge=PLUGIN)
            else:
                c.pill(px, y, label, edge=None)
            px -= 1.0
        y -= 4.6
    c.text(
        1.5,
        y - 0.6,
        "record: a signed record, kept only with a signing_key (the journal works the same without one).\n"
        "A failure of the store or of the plugin itself (storage_unavailable, plugin_error) keeps no record.",
        color=LINE,
        va="top",
    )
    end = c.legend(
        y - 7.6, parties=("plugin",), outcomes=("pass", "refused", "no", "ran", "unknown", "replay")
    )
    c.fit(end - 0.8)
    return c.save("outcomes.png")


def stopped() -> Path:
    c = Canvas(8.6)
    top = c.ymax
    c.title(
        "What stops a conversation's guarded calls, and what ends the stop",
        "Both are kept per conversation: other conversations on the same worker go on as before.",
    )
    # 1. the denial cap
    y = top - 11
    c.heading(y, "1  Consecutive Council denials (max_denials, default 3)")
    sy = y - 8
    xs = (4, 21, 38, 55)
    for i, x in enumerate(xs):
        last = i == len(xs) - 1
        c.box(x, sy - 3.2, 11, 6.4, "3" if last else str(i), "the cap" if last else "", edge=None)
        if i:
            c.arrow((xs[i - 1] + 11, sy), (x, sy))
            c.mark((xs[i - 1] + 11 + x) / 2, sy + 2.6, "no", r=1.2)
            c.text((xs[i - 1] + 11 + x) / 2, sy - 1.6, "denied", ha="center")
    c.arrow((66, sy), (77, sy))
    c.text(71.5, sy + 1.6, "each later\ncall", ha="center", va="bottom")
    c.box(77, sy - 4, 22, 8, "denial_cap", "Council not asked", edge=None, size=BODY)
    c.mark(79.2, sy + 2.0, "refused", r=1.2)
    # a valid writ resets 1 and 2 (and keeps 0)
    for x, depth in ((21, 6.5), (38, 9.5)):
        c.route([(x + 5.5, sy - 3.2), (x + 5.5, sy - depth), (9.5, sy - depth), (9.5, sy - 3.2)], color=INK)
    c.text(23.6, sy - 8.0, "a valid writ resets to 0", ha="center")
    c.text(
        60.5,
        sy - 6.6,
        "at the cap no writ can come, so in this\nconversation the count stays at the cap",
        color=LINE,
        va="top",
    )
    c.text(
        4,
        sy - 13.2,
        "A Council error, or a writ refused by the plugin, leaves the count unchanged.",
        color=LINE,
    )

    # 2. blocked
    y = sy - 18.5
    c.heading(y, "2  An outcome that may have run blocks its conversation")
    ty = y - 6
    for i, (status, sub) in enumerate(
        (
            ("evidence_invalid", "result fails verification"),
            ("execution_unknown", "no readable answer"),
            ("answer lost after T2", "row left 'dispatching'"),
        )
    ):
        yy = ty - 8.5 * i
        c.box(1.5, yy - 3.4, 27, 6.8, status, sub, edge=None, size=BODY)
        c.mark(3.7, yy + 1.4, "unknown", r=1.1)
        c.arrow((28.5, yy), (39.5, ty - 8.5), color=MUTED, lw=0.9)
    cy = ty - 8.5
    c.text(29.3, cy - 1.0, "any one", ha="left", va="top", color=LINE)
    c.ax.add_patch(Circle((44, cy), 4.5, fc=BLOCKED, ec=BLOCKED, zorder=5))
    c.text(44, cy, "blocked", color="white", weight="bold", ha="center", z=6)
    c.arrow((48.5, cy), (62, cy))
    c.text(55.25, cy + 1.6, "each later\ncall", ha="center", va="bottom")
    c.box(
        62,
        cy - 4,
        37,
        8,
        "session_requires_reconciliation",
        "nothing is retried: the call may have run",
        edge=None,
        size=BODY,
    )
    c.mark(64.2, cy + 2.2, "refused", r=1.1)

    # 3. what ends either one
    y = cy - 17.5
    c.heading(y, "3  What ends a cap or a block")
    ry = y - 6
    c.box(1.5, ry - 3.4, 34, 6.8, "operator restarts the worker", "", edge=APP, size=BODY)
    c.arrow((35.5, ry), (52, ry), once=True)
    c.box(52, ry - 3.4, 47, 6.8, "an empty store", "every conversation starts afresh", edge=None, size=BODY)
    ry -= 9.5
    c.box(
        1.5,
        ry - 3.4,
        34,
        6.8,
        "its transcript shows no live token",
        "each R (1 h) after issue, evicted or trimmed",
        edge=None,
        size=BODY,
    )
    c.arrow((35.5, ry), (52, ry))
    c.text(43.75, ry + 1.4, "next request", ha="center", va="bottom")
    c.box(
        52,
        ry - 3.4,
        47,
        6.8,
        "a new conversation",
        "count 0, not blocked; old evidence not citable",
        edge=None,
        size=BODY,
    )

    def once(x, yy):
        c.arrow((x, yy), (x + 2.6, yy), once=True)

    end = c.legend(
        ry - 7,
        outcomes=("no", "refused", "unknown"),
        parties=("app",),
        extra=((once, ARROWS["once"]),),
    )
    c.fit(end - 0.8)
    return c.save("stopped.png")


# ── 4. who signs what, and on whose word the Council is asked ─────────────


def _swatch(c: Canvas, edge, ls="-", fill="white", hatch=False):
    """A legend swatch shaped like a box: ``edge=None`` is a shaded stage; ``hatch``: nobody signs it."""

    def draw(x, yy):
        if hatch:
            c.ax.add_patch(
                FancyBboxPatch(
                    (x, yy - 0.9),
                    2.2,
                    1.8,
                    boxstyle="round,pad=0,rounding_size=0.3",
                    fc="none",
                    ec="none",
                    hatch="///",
                    hatchcolor=NEUTRAL,
                    lw=0,
                    zorder=5.5,
                )
            )
        c.ax.add_patch(
            FancyBboxPatch(
                (x, yy - 0.9),
                2.2,
                1.8,
                boxstyle="round,pad=0,rounding_size=0.3",
                fc=STAGE if edge is None else fill,
                ec="none" if edge is None else edge,
                lw=1.2,
                ls=ls,
                zorder=5,
            )
        )

    return draw


def trust() -> Path:
    c = Canvas(7.0)
    top = c.ymax
    c.title(
        "Who signs what, and what the plugin checks it against",
        "Read each row left to right. The table under this figure gives the details.",
    )
    rows = [
        (
            "Council",
            NOMOS,
            "signs",
            ("writ", "one exact call; short-lived"),
            "-",
            "checked against",
            ("the Council's key", "pinned, or fetched once (opt-in)"),
        ),
        (
            "Executor",
            NOMOS,
            "signs",
            ("ExecutionResult", "binds the writ, command, output"),
            "-",
            "checked against",
            ("pinned Executor keys", "on arrival, at every citation"),
        ),
        (
            "Plugin's key",
            PLUGIN,
            "signs",
            ("the plugin's records", "+ receipts; certifies results, decisions"),
            "-",
            "checked against",
            ("its own key", "at every catalog read, citation"),
        ),
        (
            "Model",
            ASSERTED,
            "writes",
            ("the intent", "description, justification, …"),
            "--",
            "checked by",
            ("nobody", "it never decides what runs"),
        ),
        (
            "Council",
            NOMOS,
            "returns",
            ("a denial or an error", "unsigned; no reply: the plugin's error"),
            "--",
            "kept in",
            ("a plugin-certified record", "with a signing_key"),
        ),
    ]
    y = top - 10
    for who, color, verb, (name, sub), ls, link, (cname, csub) in rows:
        c.box(1.5, y - 7, 15, 7, who, "signing_key" if color == PLUGIN else "", edge=color, size=BODY)
        c.arrow((16.5, y - 3.5), (24, y - 3.5))
        c.text(20.2, y - 1.9, verb, ha="center", color=LINE)
        c.box(24, y - 7, 33, 7, name, sub, edge=color, size=BODY, hatch=ls == "--")
        c.arrow((57, y - 3.5), (71, y - 3.5))
        c.text(64, y - 1.9, link, ha="center", color=LINE)
        c.box(71, y - 7, 28, 7, cname, csub, edge=None, size=BODY)
        y -= 9.5
    c.text(
        1.5,
        y - 0.5,
        "Certified: an ExecutionResult signed by a key the Council trusts: the Executor's, or the plugin's (registered).\n"
        "Signed is not succeeded: a verified non-zero exit is still certified.",
        color=LINE,
        va="top",
    )
    end = c.legend(
        y - 7.5,
        parties=("nomos", "plugin", "asserted"),
        extra=(
            (_swatch(c, INK), "middle box, plain: signed"),
            (_swatch(c, INK, hatch=True), "middle box, hatched: nobody signs it"),
            (_swatch(c, None), "shaded: a key or a fact, not a party"),
        ),
    )
    c.fit(end - 0.8)
    return c.save("trust.png")


def council_request() -> Path:
    c = Canvas(5.5)
    top = c.ymax
    c.title(
        "What council_authorize tells the Council, and on whose word",
        "Sent over C4 only for calls that pass the plugin's own checks. The model's intent can explain a call;\n"
        "it can never change what is authorized. Every field: the table below.",
    )
    # (class, header, sub, two lines of (text, field class) pills)
    rows = [
        (
            "observed",
            "Observed",
            "the Council judges it",
            [
                [("operator config: agent_id, council_id, auth_token", "app")],
                [("command, or tool_call", "observed"), ("context: the turn's user request", "observed")],
            ],
        ),
        (
            "asserted",
            "Asserted",
            "the Council weighs it",
            [
                [("the model's four intent fields", "asserted")],
                [],
            ],
        ),
        (
            "nomos",
            "Certified",
            "Nomos re-verifies it",
            [
                [("certified_evidence: cited Executor results, whole", "nomos")],
                [("plugin-certified: local results, decisions; tool list (opt-in)", "plugin")],
            ],
        ),
    ]
    y = top - 13.5
    H = 8.0
    for key, head, sub, lines in rows:
        color = PARTY[key][0]
        c.ax.add_patch(Rectangle((20, y - H), 62, H, fc=STAGE, ec="none", zorder=1))
        c.box(1.5, y - H, 20, H, head, sub, edge=color, size=BODY)
        c.tag(18.6, y - 1.6, key)
        for j, line in enumerate(lines):
            x = 23.5
            for text, k in line:
                ly = y - 2.4 - 3.2 * j
                if k != key:  # a field of another authority than its row: tag it
                    c.tag(x, ly, k)
                    x += 2.7
                x = c.pill(x, ly, text, edge=PARTY[k][0]) + 1.2
        y -= H + 2.5
    bottom = y + 2.5
    c.box(
        82,
        bottom,
        16.5,
        top - 13.5 - bottom,
        "council_authorize",
        "one request (C4)",
        edge=OBSERVED,
        size=BODY,
    )
    end = c.legend(bottom - 4, parties=("observed", "asserted", "nomos", "plugin", "app"), tags=True)
    c.fit(end - 0.8)
    return c.save("council-request.png")


def signing_key() -> Path:
    c = Canvas(4.5)
    top = c.ymax
    c.title(
        "What the plugin's signing key switches on",
        "The operator's signing_key is optional, and the plugin never makes one up.",
    )
    y = top - 10.5
    c.box(1.5, y - 7, 30, 7, "with signing_key", "evidence records are kept", edge=PLUGIN)
    c.box(1.5, y - 16, 30, 7, "without one", "no evidence records", edge=PLUGIN, ls="--")
    c.pill(25.5, y - 10.6, "off", edge=None, color=MUTED, weight="bold")
    for j, item in enumerate(
        [
            "signed records, a catalog, nomos.evidence citations",
            "certified local results, decisions; tool list (opt-in)",
        ]
    ):
        c.pill(42, y - 2.0 - 2.9 * j, item, edge=PLUGIN)
    c.arrow((31.5, y - 3.5), (41, y - 3.5))
    c.text(36.25, y - 2.3, "turns on", ha="center", va="bottom", color=LINE)
    c.pill(42, y - 11.0, "no records, no catalog, no evidence field", edge=PLUGIN, color=MUTED, ls="--")
    c.mark(43.3, y - 13.9, "refused", r=1.2)
    x = c.pill(45.2, y - 13.9, "a citation: missing_intent", edge=PLUGIN, color=MUTED, ls="--")
    c.pill(x + 1.2, y - 13.9, "tool_definitions off (warns)", edge=PLUGIN, color=MUTED, ls="--")
    c.arrow((31.5, y - 12.5), (41, y - 12.5))
    c.text(36.25, y - 11.3, "leaves", ha="center", va="bottom", color=LINE)
    yy = y - 21
    c.heading(yy, "The same either way")
    x = 1.5
    for item in [
        "turn tokens, conversations",
        "the journal, replay, quotas",
        "writ checks, nonce claims",
        "Executor results verified",
    ]:
        x = c.pill(x, yy - 3.4, item, edge=None) + 1.2
    end = c.legend(
        yy - 8,
        parties=("plugin",),
        outcomes=("refused",),
        extra=(
            (_swatch(c, PLUGIN, ls="--"), "dashed, 'off': switched off"),
            (_swatch(c, None), "shaded: a feature, not a party"),
        ),
    )
    c.fit(end - 0.8)
    return c.save("signing-key.png")


# ── 5. conversations sharing one worker ───────────────────────────────────


def _conv(c: Canvas, x, y, label, r=2.6):
    """A conversation: a circle in the store's colour."""
    c.ax.add_patch(Circle((x, y), r, fc="white", ec=PLUGIN, lw=1.3, zorder=5))
    c.text(x, y, label, ha="center", weight="bold", family=MONO, z=6)


def _token(c: Canvas, x, y, label, w=13):
    c.ax.add_patch(
        FancyBboxPatch(
            (x, y - 1.1),
            w,
            2.2,
            boxstyle="round,pad=0,rounding_size=0.4",
            fc="white",
            ec="none",
            zorder=4,
        )
    )
    t = c.text(x + 0.6, y, label, family=MONO, z=5)
    c.boxed.append((t, (x, y - 1.1, w, 2.2)))


def isolation() -> Path:
    c = Canvas(9.5)
    top = c.ymax
    c.title(
        "How the worker tells conversations apart",
        "Only by the turn tokens in each transcript. The conversation id (cv_…) is never shown to the model.",
    )
    y = top - 10.5
    c.heading(y, "1  H3 is given no caller identity, only the call")
    c.box(1.5, y - 11, 30, 7, "Relay host", "tool name · args · tool_call_id")
    c.arrow((31.5, y - 7.5), (42, y - 7.5))
    c.badge(36.75, y - 7.5, "C3")
    c.box(42, y - 11, 22, 7, "H3", "reads nomos.turn")
    c.arrow((64, y - 7.5), (72, y - 7.5))
    c.badge(68, y - 7.5, "C6")
    c.store(72, y - 12, 26.5, 8.5, "Evidence store", "token → its conversation")
    y -= 17.5
    c.heading(y, "2  H1: the tokens a request's own messages show pick its conversation")
    y -= 3
    panels = [
        ("No token it knows", "→ a new conversation"),
        ("Tokens of one conversation", "→ continue it"),
        ("Tokens of several", "→ merged: a new one, strictest state"),
    ]
    for i, (head, result) in enumerate(panels):
        x = 1.5 + 33 * i
        c.ax.add_patch(
            FancyBboxPatch(
                (x, y - 17),
                31.5,
                17,
                boxstyle="round,pad=0,rounding_size=0.7",
                fc=STAGE,
                ec="none",
                zorder=1,
            )
        )
        c.n_boxes += 1
        c.text(x + 1, y - 1.9, head, weight="bold")
        c.text(x + 15.75, y - 14.8, result, ha="center", weight="bold")
    yy = y - 7.5
    _token(c, 3, yy, "no nt_ token, or", w=17)
    _token(c, 3, yy - 3, "an expired one", w=17)
    c.arrow((20.3, yy - 1.5), (24.2, yy - 1.5))
    _conv(c, 27, yy - 1.5, "new")
    _token(c, 36, yy, "nt_… from 1", w=13)
    _token(c, 36, yy - 3, "nt_… from 1", w=13)
    c.arrow((49.5, yy - 1.5), (55.5, yy - 1.5))
    _conv(c, 59, yy - 1.5, "1")
    _token(c, 69, yy, "nt_… from 1", w=11.5)
    _token(c, 69, yy - 3, "nt_… from 2", w=11.5)
    _conv(c, 84, yy + 1, "1", r=1.9)
    _conv(c, 84, yy - 4, "2", r=1.9)
    c.arrow((80.5, yy), (82.1, yy + 0.6), lw=0.8, color=MUTED)
    c.arrow((80.5, yy - 3), (82.1, yy - 3.6), lw=0.8, color=MUTED)
    c.arrow((85.9, yy + 0.4), (91.6, yy - 1.0))
    c.arrow((85.9, yy - 3.4), (91.6, yy - 2.0))
    _conv(c, 94.4, yy - 1.5, "3")
    y -= 19
    end = c.legend(
        y,
        parties=("observed", "plugin"),
        arrows=("call",),
        extra=((_swatch(c, None), "shaded: a case or a fact, not a party"),),
    )
    c.fit(end - 0.8)
    return c.save("isolation.png")


def conversations() -> Path:
    """Two conversations on one worker, from the capture: what each may see and cite."""
    d = ex("conversations")
    store = d["store"]
    short = lambda v: v[:9] + "…"  # noqa: E731
    turns = {t["token"]: t for t in store["turns"]}
    a_turn = [t for t in turns if turns[t]["user_request"].startswith("Alice")]
    b_turn = [t for t in turns if turns[t]["user_request"].startswith("Bob")]
    a_ev = d["alice"]["tool_calls"][0]["result"]["evidence_id"]
    b_ev = d["bob"]["tool_calls"][0]["result"]["evidence_id"]
    refused = d["bob"]["tool_calls"][1]["result"]["status"]
    c = Canvas(10.5)
    top = c.ymax
    c.title(
        "What a conversation may see and cite",
        "The rule, then two conversations captured from a real run on one worker.",
    )
    y = top - 10
    c.heading(y, "1  Listed in the catalog, or only citable")
    cy = y - 10
    c.ax.add_patch(Ellipse((19, cy), 34, 11, fc=OBSERVED, alpha=0.07, ec=OBSERVED, lw=1.2, zorder=1))
    c.ax.add_patch(Ellipse((43, cy), 34, 11, fc=PLUGIN, alpha=0.07, ec=PLUGIN, lw=1.2, zorder=1))
    c.text(2, cy + 6.6, "ids this request's messages show", color=OBSERVED)
    c.text(60, cy + 6.6, "records this turn may cite", ha="right", color=PLUGIN)
    c.text(11.5, cy, "not citable", ha="center", color=MUTED)
    c.text(31, cy, "citable, and\nlisted if a result*", ha="center", weight="bold")
    c.text(50.5, cy, "citable,\nnot listed", ha="center")
    c.text(
        63,
        cy,
        "Citable: this conversation's records, or those\nmerged into it, unless expired or quarantined.",
        color=LINE,
    )
    c.text(63, cy - 4.4, "* a result, a denial or a Council error", color=LINE)
    y = cy - 9
    c.heading(y, "2  Alice and Bob, one worker")

    def column(x, name, steps):
        c.text(x, y - 3.5, f"{name}'s transcript", weight="bold")
        yy = y - 5.5
        for i, (title, sub, edge, mark) in enumerate(steps):
            if i:
                c.arrow((x + 23.5, yy), (x + 23.5, yy - 1.6), color=MUTED, lw=0.9)
                yy -= 1.6
            c.box(x, yy - 6.4, 47, 6.4, title, sub, edge=edge, size=BODY)
            if mark:
                c.mark(x + 2.6, yy - 3.2, mark)
            yy -= 6.4
        return yy

    column(
        1.5,
        "Alice",
        [
            ("user", "Alice: check that the shell works.", APP, None),
            ("Nomos message (H1)", f"turn {short(a_turn[0])}", OBSERVED, None),
            ("tool call (model)", f"/bin/echo alice · nomos.turn = {short(a_turn[0])}", ASSERTED, None),
            ("tool result (H3)", f"executed · {short(a_ev)}", PLUGIN, "ran"),
            (
                "next Nomos message (H1)",
                f"new turn {short(a_turn[1])} · catalog: {short(a_ev)}",
                OBSERVED,
                None,
            ),
        ],
    )
    low = column(
        51.5,
        "Bob",
        [
            ("user", "Bob: check that the shell works.", APP, None),
            ("Nomos message (H1)", f"turn {short(b_turn[0])}", OBSERVED, None),
            ("tool call (model)", f"/bin/echo bob · nomos.turn = {short(b_turn[0])}", ASSERTED, None),
            ("tool result (H3)", f"executed · {short(b_ev)}", PLUGIN, "ran"),
            (
                "next Nomos message (H1)",
                f"new turn {short(b_turn[1])} · catalog: {short(b_ev)}",
                OBSERVED,
                None,
            ),
            ("tool call (model)", f"cites Alice's {short(a_ev)}", ASSERTED, None),
            (f"refused: {refused}", "the Council is not asked", PLUGIN, "refused"),
        ],
    )
    end = c.legend(low - 4, parties=("observed", "asserted", "plugin", "app"), outcomes=("ran", "refused"))
    c.fit(end - 0.8)
    return c.save("conversations.png")


# ── 6. how long evidence lasts ────────────────────────────────────────────


def store() -> Path:
    c = Canvas(6.5)
    top = c.ymax
    c.title(
        "How long the evidence lasts",
        "The store is SQLite :memory: inside the worker: no file on disk, no service. R = limits.retention_seconds (1 h).",
    )
    y = top - 10.5
    c.heading(y, "1  The store lives exactly as long as the worker process")
    y -= 3
    stages = [
        ("Worker starts", "an empty store", None),
        ("Serving", "every conversation", None),
        ("Worker exits", "the whole store is gone", None),
        ("New activation", "old ids: evidence_not_found", None),
    ]
    for i, (name, sub, color) in enumerate(stages):
        x = 1.5 + 25 * i
        c.box(x, y - 7, 22.5, 7, name, sub, edge=color, size=BODY)
        if i:
            c.arrow((x - 2.5, y - 3.5), (x, y - 3.5), color=MUTED, lw=0.9, once=i == len(stages) - 1)
    c.text(
        98.5,
        y - 9.6,
        "only if an operator restarts it: Relay never respawns a worker",
        ha="right",
        color=MUTED,
        style="italic",
    )
    y -= 16
    c.heading(y, "2  How long each row lives inside it")
    y -= 3.5
    x0, xR, x2R, xend = 22, 50, 78, 98.5
    for x, label in ((x0, "written"), (xR, "+ R"), (x2R, "+ 2R")):
        c.text(x, y, label, ha="center", color=MUTED)
        c.ax.plot([x, x], [y - 1.5, y - 22], color=NEUTRAL, lw=0.9, ls=":", zorder=1)
    rows = [
        (
            "record",
            [
                (x0, xR, "content: citable", PLUGIN, "white"),
                (xR, x2R, "tombstone: evidence_expired", STAGE, INK),
            ],
            "row gone",
        ),
        ("turn (token)", [(x0, xR, "live", PLUGIN, "white")], "gone: unknown_turn"),
        (
            "completed call",
            [(x0, xR, "replays in its conversation", PLUGIN, "white")],
            "then the same id is a new call",
        ),
        (
            "writ claim",
            [(x0, xend, "kept for the worker's whole life: a nonce is never reusable", PLUGIN, "white")],
            "",
        ),
    ]
    for i, (name, bars, after) in enumerate(rows):
        ry = y - 4 - 5 * i
        c.text(1.5, ry, name, weight="bold")
        for xa, xb, label, fc, tc in bars:
            c.ax.add_patch(Rectangle((xa, ry - 1.4), xb - xa, 2.8, fc=fc, ec="white", lw=1.0, zorder=2))
            t = c.text(xa + 1, ry, label, color=tc, z=5)
            c.boxed.append((t, (xa, ry - 1.4, xb - xa, 2.8)))
        if after:
            c.text(bars[-1][1] + 1.2, ry, after, color=MUTED, style="italic")

    def once(x, yy):
        c.arrow((x, yy), (x + 2.6, yy), once=True, color=MUTED, lw=0.9)

    end = c.legend(
        y - 25,
        parties=("plugin",),
        extra=(
            (_swatch(c, None), "shaded: a stage, not a party"),
            (once, ARROWS["once"]),
        ),
        cols=3,
    )
    c.fit(end - 0.8)
    return c.save("store.png")


# ── 7. the call journal ───────────────────────────────────────────────────

ORANGE = "#B7791F"


def journal() -> Path:
    c = Canvas(9.0)
    top = c.ymax
    c.title(
        "When the call journal is written",
        "Time runs left to right; the numbers are the sequence figures'. Shaded: the two windows a failure can hit\n"
        "(what each failure leaves behind: the table under this figure).",
    )
    lanes = {
        "council": (top - 15, "Council", NOMOS),
        "run": (top - 27, "Executor", NOMOS),
        "h3": (top - 40, "H3 + writ\nhandler", OBSERVED),
        "store": (top - 54, "Evidence\nstore", PLUGIN),
    }
    Y = {k: v[0] for k, v in lanes.items()}
    sy = Y["store"]
    x1, x2, x3 = 24, 60, 88
    for xa, xb, color, label in (
        (x1 + 1, x2 - 1, ORANGE, "window 1: after T1, before T2"),
        (x2 + 1, x3 - 1, BLOCKED, "window 2: after T2, before T3"),
    ):
        c.ax.add_patch(
            Rectangle((xa, sy - 3), xb - xa, Y["council"] + 6 - sy, fc=color, alpha=0.09, ec="none", zorder=0)
        )
        c.text((xa + xb) / 2, Y["council"] + 4.6, label, ha="center", weight="bold", color=color)
    for key, (y, name, color) in lanes.items():
        xa = 62 if key == "run" else 18
        c.ax.plot([xa, 87 if key == "run" else 99], [y, y], color="#D9E2EC", lw=1.2, zorder=0.5)
        c.box(1.5, y - 3.5, 14, 7, name, edge=color, size=BODY)
    for xa, xb, state, shade in (
        (x1, x2, "authorizing", "#D5EEF2"),
        (x2, x3, "dispatching", "#A9DCE5"),
        (x3, 99, "complete", "#7CC8D6"),
    ):
        c.ax.add_patch(Rectangle((xa, sy - 6.5), xb - xa, 3, fc=shade, ec=PLUGIN, lw=0.8, zorder=1))
        c.text((xa + xb) / 2, sy - 5, state, ha="center", z=5)
    c.text(18.5, sy - 5, "no row", color=MUTED, style="italic")
    for x, name, body in (
        (x1, "13  T1", "checks, then the row"),
        (x2, "19  T2", "nonce, then dispatching"),
        (x3, "23  T3", "record + row, at once"),
    ):
        c.ax.add_patch(
            FancyBboxPatch(
                (x - 1, sy - 2.5),
                2,
                5,
                boxstyle="round,pad=0,rounding_size=0.4",
                fc=PLUGIN,
                ec=PLUGIN,
                zorder=3,
            )
        )
        c.arrow((x, Y["h3"]), (x, sy + 2.5), ring=True, z=4)
        c.badge(x, (Y["h3"] + sy) / 2 + 1, "C6")
        c.text(x + 2.6, (Y["h3"] + sy) / 2 + 1, name, weight="bold", color=PLUGIN)
        c.text(x if x < x3 else 99, sy - 8.8, body, ha="center" if x < x3 else "right")
    c.arrow((31, Y["h3"]), (31, Y["council"]), ring=True)
    c.badge(31, Y["h3"] + 4.2, "C4")
    c.text(29.5, Y["council"] - 3.5, "16  ask", ha="right")
    c.arrow((39, Y["council"]), (39, Y["h3"]), reply=True, ring=True)
    c.badge(39, Y["h3"] + 4.2, "C4")
    c.text(40.5, Y["council"] - 3.5, "17  decision")
    c.diamond(46, Y["h3"])
    c.text(46, Y["h3"] + 2.4, "18  verify", ha="center", va="bottom")
    c.mark(43, Y["h3"] - 3.2, "no", r=1.3)
    c.mark(53, Y["h3"] - 3.2, "refused", r=1.3)
    c.text(73, Y["h3"] - 3.2, "skip T2: straight to T3", ha="center", color=LINE, bg="white")
    for xa in (39.4, 47.3):  # ✕ leaves at 17, before the writ is read; ⊘ at 18
        c.ax.plot([xa, xa + 2.2], [Y["h3"] - 0.9, Y["h3"] - 3.2], color=MUTED, lw=1.0, ls=":", zorder=3)
    c.ax.plot([41.6, 84.5], [Y["h3"] - 3.2, Y["h3"] - 3.2], color=MUTED, lw=1.0, ls=":", zorder=3)
    c.arrow((84.5, Y["h3"] - 3.2), (86.6, Y["h3"] - 0.5), color=MUTED, lw=1.0)
    c.arrow((67, Y["h3"]), (67, Y["run"]))
    c.badge(67, (Y["h3"] + Y["run"]) / 2, "C5")
    c.text(65.5, Y["run"] - 2.5, "20  POST", ha="right")
    c.arrow((76, Y["run"]), (76, Y["h3"]), reply=True)
    c.badge(76, (Y["h3"] + Y["run"]) / 2, "C5")
    c.text(77.5, Y["run"] - 2.5, "21  result")
    c.text(
        63, Y["run"] + 6, "local tools: at 20, next_\nthrough Relay, no Executor", color=MUTED, style="italic"
    )
    c.arrow((92, Y["h3"]), (99, Y["h3"]), reply=True)
    c.text(99, Y["h3"] + 2.0, "24  returned", ha="right", va="bottom")
    low = sy - 9.5

    def dotted(x, yy):
        c.ax.plot([x, x + 2.4], [yy, yy], color=MUTED, lw=1.2, ls=":", zorder=3)

    end = c.legend(
        low - 2,
        parties=("observed", "nomos", "plugin"),
        arrows=("call", "reply"),
        outcomes=("no", "refused"),
        extra=((dotted, "dotted: an ending that skips T2"),),
    )
    c.fit(end - 0.8)
    return c.save("journal.png")


# ── 8. one record's life ──────────────────────────────────────────────────


def record_lifecycle() -> Path:
    c = Canvas(5.5)
    top = c.ymax
    c.title(
        "What happens to one evidence record, from build to expiry",
        "Only with a signing key: without one, no record exists. Refusals, unknown outcomes and\n"
        "quarantined results are stored but never offered (step 3).",
    )
    stages = [
        ("1 Build, seal", "plugin- or Executor-signed,\nplus a plugin receipt on all", PLUGIN),
        ("2 Persist", "before the model sees it: at\nT3, or alone if refused early", PLUGIN),
        ("3 Offer", "results, denials, errors only:\nid, tool, args; never output", OBSERVED),
        ("4 Cite", "the model names the id in\na later call's nomos", ASSERTED),
        ("5 Re-check", "receipt re-checked, certified\nre-verified on current keys", OBSERVED),
        ("6 Mark, submit", "→ submitted_unknown,\nthen sent to the Council", PLUGIN),
        ("7 Settle", "a valid writ → accepted;\nnever downgraded", PLUGIN),
        ("8 Expire", "content erased, then the id\n(timing: the store figure)", PLUGIN),
    ]
    W, gap = 23, 1.67
    h = 3.2 + 2.3 * 2 + 2.0
    places = [(1.5 + i * (W + gap), top - 9) for i in range(4)] + [
        (1.5 + (3 - i) * (W + gap), top - 9 - h - 6) for i in range(4)
    ]
    for (title, body, color), (x, t) in zip(stages, places, strict=True):
        c.card(x, t, W, title, body, color, h=h)
    for i in range(7):
        (xa, ta), (xb, tb) = places[i], places[i + 1]
        if ta == tb:
            a, b = (
                ((xa + W, ta - h / 2), (xb, tb - h / 2))
                if xb > xa
                else ((xa, ta - h / 2), (xb + W, tb - h / 2))
            )
        else:
            a, b = (xa + W / 2, ta - h), (xb + W / 2, tb)
        c.arrow(a, b, color=MUTED, lw=0.9)
    end = c.legend(top - 9 - 2 * h - 10, parties=("observed", "asserted", "plugin"))
    c.fit(end - 0.8)
    return c.save("record-lifecycle.png")


# ── 9. one approved call's data, hop by hop ───────────────────────────────


def _short(value, n=30):
    text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    text = text.replace("\n", "\\n")
    return text if len(text) <= n else text[: n - 1] + "…"


def lineage() -> Path:
    start = ex("tool_events")[0]["data"]
    request = ex("council_requests")[0]
    decision = ex("council_decisions")[0]
    writ = ex("writ_example")["decoded"]
    result = ex("executor_results")[0]["result"]
    record = ex("evidence_records")[0]["record"]
    out = ex("tool_results")[0]["result"]
    ob, asr, nm, pl = "observed", "asserted", "nomos", "plugin"
    cards = [
        (
            "the model's tool call",
            [("command", _short(start["command"], 18), ob), ("nomos.description …", "4 intent fields", asr)],
        ),
        ("H3 splits it", [("arguments", "without nomos", ob), ("journal", "authorizing", pl)]),
        (
            "council_authorize (C4)",
            [
                ("command", _short(request["command"], 18), ob),
                ("description …", "the intent", asr),
            ],
        ),
        (
            "the Council's answer (C4)",
            [("status", decision["status"], nm), ("writ", _short(decision["writ"], 14), nm)],
        ),
        (
            "the writ, verified",
            [("command", _short(writ["command"], 18), nm), ("nonce", _short(writ["nonce"], 14), nm)],
        ),
        ("POST /execute (C5)", [("writ", "unchanged", nm), ("nonce", "claimed (T2)", pl)]),
        (
            "ExecutionResult (C5)",
            [("stdout", _short(result["stdout"], 14), nm), ("signature", "Executor key", nm)],
        ),
        (
            "the record (T3)",
            [
                ("evidence_id", _short(record["evidence_id"], 12), pl),
                ("certified", str(record["certified"]).lower(), pl),
            ],
        ),
        (
            "what the model reads",
            [
                ("status", out["status"], pl),
                ("stdout", _short(out["stdout"], 14), nm),
            ],
        ),
    ]
    c = Canvas(9.5)
    top = c.ymax
    c.title(
        "How one approved call's data travels, hop by hop",
        "Recorded from a real run. A field's tag says whose word it is; follow the numbers.",
    )
    W, xs = 47.5, (1.5, 51)
    order = [(0, 0), (0, 1), (1, 1), (1, 0), (2, 0), (2, 1), (3, 1), (3, 0), (4, 0)]
    H = 3.2 + 2.9 * 2 + 1.0
    row_top = {r: top - 9 - r * (H + 4.5) for r in range(5)}
    for i, ((title, fields), (r, col)) in enumerate(zip(cards, order, strict=True)):
        x, t = xs[col], row_top[r]
        c.ax.add_patch(
            FancyBboxPatch(
                (x, t - H),
                W,
                H,
                boxstyle="round,pad=0,rounding_size=0.7",
                fc="white",
                ec=INK,
                lw=1.2,
                zorder=2,
            )
        )
        c.ax.add_patch(
            FancyBboxPatch(
                (x, t - 3.2), W, 3.2, boxstyle="round,pad=0,rounding_size=0.7", fc=INK, ec=INK, zorder=2
            )
        )
        c.n_boxes += 1
        c.panels.append((title, (x, t - H, W, H - 3.2)))
        c.text(x + 1, t - 1.6, f"{i + 1}  {title}", weight="bold", color="white")
        for j, (key, value, k) in enumerate(fields):
            yy = t - 5.2 - 2.9 * j
            c.tag(x + 0.8, yy, k)
            kt = c.text(x + 3.6, yy, key, family=MONO, color=PARTY[k][0], weight="bold")
            del kt
            c.text(x + 3.6 + 0.86 * len(key) + 1.8, yy, "= " + value, family=MONO)
    for i in range(len(cards) - 1):
        (ra, ca), (rb, cb) = order[i], order[i + 1]
        if ra == rb:
            yy = row_top[ra] - H / 2
            xa, xb = (xs[0] + W, xs[1]) if cb > ca else (xs[1], xs[0] + W)
            c.arrow((xa, yy), (xb, yy), color=MUTED, lw=0.9)
        else:
            x = xs[ca] + W / 2
            c.arrow((x, row_top[ra] - H), (x, row_top[rb]), color=MUTED, lw=0.9)
    c.text(
        xs[1],
        row_top[4] - 3,
        "Next request: the catalog lists the record\nby id, tool and arguments, never the output.",
        color=LINE,
    )
    end = c.legend(row_top[4] - H - 3.5, parties=("observed", "asserted", "nomos", "plugin"), tags=True)
    c.fit(end - 0.8)
    return c.save("lineage.png")


ALL = (
    overview,
    connections,
    h1,
    h3_authorize,
    h3_dispatch,
    outcomes,
    stopped,
    trust,
    council_request,
    signing_key,
    isolation,
    conversations,
    store,
    journal,
    record_lifecycle,
    lineage,
)

if __name__ == "__main__":
    wanted = set(sys.argv[1:])
    for fn in ALL:
        if not wanted or fn.__name__ in wanted:
            print(fn())
