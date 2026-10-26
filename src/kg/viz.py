"""A self-contained HTML view of the graph for the demo (vis-network from a CDN).

It shows a semantic view: Mention and Turn nodes are collapsed so each Assertion points straight
at the person it is about and the call it came from; hovering an Assertion shows its quote.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from kg.graph import out_nodes, referent

SHOWN = {"Account", "Contact", "User", "Opportunity", "Call", "Competitor", "Concept", "Person", "Task", "Assertion"}
COLORS = {
    "Account": "#4e79a7",
    "Contact": "#59a14f",
    "User": "#9c755f",
    "Opportunity": "#f28e2b",
    "Call": "#76b7b2",
    "Competitor": "#e15759",
    "Concept": "#b07aa1",
    "Person": "#bab0ac",
    "Task": "#edc948",
    "Assertion": "#ff9da7",
}


def _label(d: dict) -> str:
    if d["label"] == "Assertion":
        mark = {"negated": "NOT ", "hypothetical": "IF "}.get(d["modality"], "")
        return f"{d['predicate']}: {mark}{d['value']}"[:40]
    return str(d.get("name") or d.get("title") or d.get("subject") or "")[:40]


def write_html(g: nx.MultiDiGraph, path: Path) -> None:
    nodes, edges = [], []
    for n, d in g.nodes(data=True):
        if d["label"] not in SHOWN:
            continue
        title = d["label"]
        if d["label"] == "Assertion":
            turn = out_nodes(g, n, "EVIDENCED_BY")[0]
            quote = g.get_edge_data(n, turn, key="EVIDENCED_BY")["quote"]
            title = f'{d["predicate"]} [{d["modality"]}, {d["status"]}, {d["speaker_side"]}]\n"{quote}"\n{turn}'
        nodes.append(
            {
                "id": n,
                "label": _label(d),
                "group": d["label"],
                "title": title,
                "color": COLORS[d["label"]],
                "shape": "box" if d["label"] == "Assertion" else "dot",
                "borderWidth": 3 if d.get("status") == "needs_review" else 1,
            }
        )
    for s, t, k in g.edges(keys=True):
        etype = str(k).split(":")[0]
        if g.nodes[s]["label"] in SHOWN and g.nodes[t]["label"] in SHOWN:
            edges.append({"from": s, "to": t, "label": etype})
    for n, d in g.nodes(data=True):
        if d["label"] != "Assertion":
            continue
        call = f"Call:{d['call_id']}"
        edges.append({"from": n, "to": call, "label": "FROM_CALL", "dashes": True})
        for m in out_nodes(g, n, "SUBJECT"):
            if who := referent(g, m):
                edges.append({"from": n, "to": who, "label": "SUBJECT"})

    # Quotes are transcript text, which is untrusted input: escape the characters that could close the <script> block
    # ("</script>") or open a comment, so a transcript can never inject markup into the page.
    payload = json.dumps({"nodes": nodes, "edges": edges})
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Account knowledge graph</title>
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>body{{margin:0;font-family:sans-serif}}#g{{width:100vw;height:100vh}}#k{{position:fixed;top:8px;left:8px;background:#fff8;padding:6px;font-size:12px}}</style>
</head><body><div id="k">{" ".join(f'<span style="color:{c}">&#9679;</span>{lbl}' for lbl, c in COLORS.items())}
<br>thick border = needs review; NOT/IF = negated/hypothetical; hover an assertion for its quote</div><div id="g"></div>
<script>
const data = {payload};
new vis.Network(document.getElementById('g'), {{nodes: new vis.DataSet(data.nodes), edges: new vis.DataSet(data.edges)}},
  {{edges: {{arrows: 'to', font: {{size: 9, align: 'middle'}}, color: '#999'}}, nodes: {{font: {{size: 12}}}},
   physics: {{solver: 'forceAtlas2Based', stabilization: {{iterations: 300}}}}}});
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)
