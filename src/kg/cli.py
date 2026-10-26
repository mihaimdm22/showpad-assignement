"""Command line: extract -> build -> brief -> eval.

uv run kg extract            call the LLM for every transcript, save raw outputs under runs/<run>/
uv run kg build              validate + resolve + load a saved run into the graph; write out/graph.{json,html}
uv run kg brief              print the deal brief for the most stalled open opportunity
uv run kg eval               score the run against data/gold/answer_key.json
uv run kg demo               build + brief + eval on the saved v3 run (no API key needed)
uv run kg ask "question"     answer a question in plain language; Claude calls the queries above as tools
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from dotenv import dotenv_values

from kg import queries
from kg.ask import ask
from kg.data import REPO_ROOT, load_config, load_crm, load_gold, load_transcripts
from kg.evaluate import evaluate, format_report
from kg.extract import DEFAULT_MODEL, PROMPT_VERSION, load_run, run_extraction
from kg.graph import BuildResult, build_graph, export_json, out_nodes
from kg.viz import write_html

RUNS = REPO_ROOT / "runs"
OUT = REPO_ROOT / "out"
# The run the README and slides quote. File times are not a safe default: after a git clone every run looks equally new.
DEFAULT_RUN = f"{PROMPT_VERSION}__{DEFAULT_MODEL}"


def _latest_run() -> Path:
    runs = [p for p in RUNS.glob("*") if p.is_dir() and any(p.glob("call_*.json"))]
    if not runs:
        raise SystemExit("No saved runs in runs/. Run `uv run kg extract` first (needs ANTHROPIC_API_KEY).")
    return max(runs, key=lambda p: max(f.stat().st_mtime for f in p.glob("call_*.json")))


def _run_dir(arg: str | None) -> Path:
    if not arg:
        return RUNS / DEFAULT_RUN if (RUNS / DEFAULT_RUN).is_dir() else _latest_run()
    return Path(arg) if Path(arg).is_absolute() else RUNS / arg


def _build(run_dir: Path) -> tuple[BuildResult, date]:
    crm, cfg = load_crm(), load_config()
    build = build_graph(crm, cfg, load_transcripts(), load_run(run_dir))
    return build, crm.export_ts.date()


def _stalled_opportunity(build: BuildResult, as_of: date) -> str:
    g = build.graph
    open_opps = [o for o in queries.opportunities(g) if not g.nodes[o]["is_closed"]]
    return max(open_opps, key=lambda o: queries.stall(g, o, as_of)["days_in_stage"] or -1)


def _who(g, uid: str | None) -> str:
    return f"{g.nodes[uid]['name']} ({uid})" if uid else "-"


def cmd_extract(args: argparse.Namespace) -> None:
    crm, cfg = load_crm(), load_config()
    transcripts = [t for t in load_transcripts() if not args.calls or t.call_id in args.calls]
    out = RUNS / (args.run or f"{PROMPT_VERSION}__{args.model}")
    cascade = f", hard calls -> {args.escalate_to}" if args.escalate_to else ""
    print(
        f"Extracting {len(transcripts)} call(s) with {args.model}{cascade} (effort={args.effort}, prompt={PROMPT_VERSION}) -> {out}"
    )
    run_extraction(transcripts, crm, cfg, out, model=args.model, effort=args.effort, escalate_to=args.escalate_to)


def cmd_build(args: argparse.Namespace) -> BuildResult:
    run_dir = _run_dir(args.run)
    build, _ = _build(run_dir)
    g = build.graph
    print(f"Run {run_dir.name}: graph has {g.number_of_nodes()} nodes, {g.number_of_edges()} edges")
    for call_id, rep in build.reports.items():
        v, r = rep.validated, rep.resolution
        print(f"\n{call_id}  [{rep.meta.get('model_served')}, {rep.meta.get('prompt_version')}]")
        print(f"  extracted {len(v.items)} claims + {len(v.mentions)} person mentions; rejected {len(v.rejections)}")
        for rej in v.rejections:
            print(f"    rejected {rej.source}: {rej.reason} ({rej.detail})")
        for spk, link in r.speakers.items():
            print(f"  speaker {spk:6} -> {link.decision:9} {_who(g, link.target)} via {link.method}")
        for m in v.mentions:
            link = r.mentions[m.mention_id]
            cands = f" candidates={link.candidates}" if link.decision == "candidate" else ""
            print(
                f"  mention {m.turn_id} {m.surface!r:22} ({m.side}, {m.mention_type}) -> {link.decision:9} "
                f"{_who(g, link.target)} score={link.score}{cands}"
            )
    export_json(g, OUT / "graph.json")
    write_html(g, OUT / "graph.html")
    print("\nWrote out/graph.json and out/graph.html")
    return build


def cmd_brief(args: argparse.Namespace) -> None:
    build, as_of = _build(_run_dir(args.run))
    opp = f"Opportunity:{args.opp}" if args.opp else _stalled_opportunity(build, as_of)
    viewer = f"User:{args.as_user}" if args.as_user else None
    print(queries.deal_brief(build.graph, opp, as_of, viewer))


def cmd_eval(args: argparse.Namespace) -> None:
    run_dir = _run_dir(args.run)
    build, as_of = _build(run_dir)
    report = evaluate(build, load_gold(), as_of)
    (run_dir / "eval.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(format_report(report))


def cmd_ask(args: argparse.Namespace) -> None:
    build, as_of = _build(_run_dir(args.run))
    g = build.graph
    viewer = f"User:{args.as_user}" if args.as_user else out_nodes(g, _stalled_opportunity(build, as_of), "OWNED_BY")[0]
    answer, calls = ask(args.question, g, as_of, viewer, model=args.model, effort=args.effort)
    print(f"Asked as {g.nodes[viewer]['name']}. Tools called: " + ("; ".join(calls) or "none"))
    print("\n" + answer)


def cmd_demo(args: argparse.Namespace) -> None:
    cmd_build(args)
    print("\n" + "=" * 100)
    cmd_brief(args)
    print("\n" + "=" * 100)
    cmd_eval(args)


def _load_env(path: Path = REPO_ROOT / ".env") -> None:
    """Load KEY=value pairs from .env without overriding the shell. Empty values are skipped, so the blank
    placeholder never shadows another credential source the Anthropic SDK could use."""
    for key, value in dotenv_values(path).items():
        if value and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    _load_env()
    p = argparse.ArgumentParser(prog="kg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="run LLM extraction and save raw outputs")
    e.add_argument("--model", default=DEFAULT_MODEL)
    e.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    e.add_argument("--run", help="run directory name under runs/ (default: <prompt>__<model>)")
    e.add_argument("--calls", nargs="*", help="only these call IDs")
    e.add_argument("--escalate-to", help="cascade: send calls with a known hard case (shared first name) to this model")
    e.set_defaults(fn=cmd_extract)
    a = sub.add_parser("ask", help="answer a question in plain language from the graph")
    a.add_argument("question")
    a.add_argument("--run", help=f"run directory under runs/ (default: {DEFAULT_RUN})")
    a.add_argument(
        "--as", dest="as_user", help="Salesforce User Id of the seller asking (default: owner of the most stalled deal)"
    )
    a.add_argument("--model", default=DEFAULT_MODEL)
    a.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    a.set_defaults(fn=cmd_ask)
    for name, fn, help_ in (
        ("build", cmd_build, "load a saved run into the graph"),
        ("brief", cmd_brief, "print a deal brief"),
        ("eval", cmd_eval, "score a saved run against the gold key"),
        ("demo", cmd_demo, "build + brief + eval"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--run", help=f"run directory under runs/ (default: {DEFAULT_RUN})")
        if name in ("brief", "demo"):
            s.add_argument("--opp", help="Salesforce Opportunity Id (default: the open opportunity longest in stage)")
            s.add_argument(
                "--as", dest="as_user", help="Salesforce User Id of the seller reading the brief (default: deal owner)"
            )
        s.set_defaults(fn=fn)
    args = p.parse_args()
    args.fn(args)
