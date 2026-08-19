#!/usr/bin/env python3
"""Aggregate enriched incremental-game reviews and render the HTML report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb

from games_analytics.config import Settings


APPIDS = (2666510, 1473350)
GAME_NAMES = {2666510: "Rusty's Retirement", 1473350: "(the) Gnorp Apologue"}
DATA_DIR = Path("data/analysis/incremental-cross-game-2026-08-16")
REPORT_JSON = Path("reports/incremental-games-review-analysis.json")
REPORT_HTML = Path("reports/incremental-games-review-analysis.html")

LABEL_NAMES = {
    "gameplay.core_loop": "Core loop",
    "gameplay.pacing": "Pacing",
    "gameplay.progression": "Progression",
    "gameplay.replayability": "Replayability",
    "gameplay.balance": "Balance",
    "gameplay.difficulty": "Difficulty",
    "gameplay.controls": "Controls",
    "gameplay.level_design": "Run / level structure",
    "gameplay.build_variety": "Build variety",
    "gameplay.grind": "Grind",
    "content.content_amount": "Amount of content",
    "content.content_variety": "Content variety",
    "content.endgame": "Endgame",
    "presentation.ui": "Interface",
    "presentation.ux": "User experience",
    "presentation.art_style": "Art style",
    "presentation.music": "Music",
    "presentation.audio": "Audio",
    "technical.performance": "Performance",
    "technical.bugs": "Bugs",
    "technical.crashes": "Crashes",
    "technical.save_system": "Save system",
    "product.price": "Price",
    "product.value": "Value",
    "product.updates": "Updates",
    "product.developer_support": "Developer support",
    "product.missing_features": "Missing features",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def label_for_statement(value: dict[str, Any]) -> str:
    label = value["l"]
    return f"{label}:{value['n']}" if value.get("n") else label


def label_for_aspect(value: dict[str, Any]) -> str:
    label = f"{value['c']}.{value['s']}"
    return f"{label}:{value['n']}" if value.get("n") else label


def display_label(label: str) -> str:
    if label in LABEL_NAMES:
        return LABEL_NAMES[label]
    base, _, novel = label.partition(":")
    if novel:
        return novel.replace("_", " ").title()
    return base.split(".", 1)[-1].replace("_", " ").title()


def category(label: str) -> str:
    return label.split(".", 1)[0]


def rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def top_rows(counter: Counter[str], total: int, limit: int = 12) -> list[dict[str, Any]]:
    return [
        {"label": label, "name": display_label(label), "count": count, "rate": rate(count, total)}
        for label, count in counter.most_common(limit)
    ]


def aggregate() -> dict[str, Any]:
    manifest = load_json(DATA_DIR / "manifest.json")
    cohort_results = {
        "negative": load_json(DATA_DIR / "negative-result.json"),
        "positive": load_json(DATA_DIR / "positive-result.json"),
    }
    all_ids = [recommendation_id for result in cohort_results.values() for recommendation_id in result["outputs"]]
    connection = duckdb.connect(str(Settings().duckdb_path), read_only=True)
    try:
        placeholders = ",".join("?" for _ in all_ids)
        rows = connection.execute(
            f"""SELECT recommendation_id,appid,votes_up,playtime_at_review_minutes,
                       timestamp_created,language
                FROM reviews WHERE recommendation_id IN ({placeholders})""",
            all_ids,
        ).fetchall()
        trends = connection.execute(
            """SELECT appid,year(timestamp_created) AS year,count(*) AS reviews,
                      sum(CASE WHEN voted_up THEN 0 ELSE 1 END) AS negative
               FROM reviews WHERE appid IN (?,?)
               GROUP BY 1,2 ORDER BY 1,2""",
            list(APPIDS),
        ).fetchall()
    finally:
        connection.close()
    metadata = {
        recommendation_id: {
            "appid": appid,
            "votes_up": int(votes_up or 0),
            "playtime": playtime,
            "timestamp": timestamp.isoformat() if timestamp else None,
            "language": language,
        }
        for recommendation_id, appid, votes_up, playtime, timestamp, language in rows
    }

    counts: dict[int, dict[str, dict[str, Counter[str]]]] = {
        appid: {
            cohort: {
                "complaints": Counter(), "praises": Counter(), "requests": Counter(),
                "technical": Counter(), "aspects_negative": Counter(),
                "aspects_positive": Counter(), "categories": Counter(), "discoveries": Counter(),
            }
            for cohort in cohort_results
        }
        for appid in APPIDS
    }
    valid: dict[int, Counter[str]] = {appid: Counter() for appid in APPIDS}
    examples: dict[str, Any] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    )
    sentiment: dict[int, dict[str, Counter[str]]] = {
        appid: {cohort: Counter() for cohort in cohort_results} for appid in APPIDS
    }

    bucket_map = {"co": "complaints", "pr": "praises", "fr": "requests", "ti": "technical"}
    for cohort, result in cohort_results.items():
        for recommendation_id, item in result["outputs"].items():
            appid = metadata[recommendation_id]["appid"]
            valid[appid][cohort] += 1
            sentiment[appid][cohort][item["s"]] += 1
            for key, bucket in bucket_map.items():
                labels_seen: set[str] = set()
                for value in item.get(key, []):
                    label = label_for_statement(value)
                    labels_seen.add(label)
                    examples[cohort][bucket][label][appid].append(
                        (metadata[recommendation_id]["votes_up"], recommendation_id, value["t"])
                    )
                    if ":" in label:
                        counts[appid][cohort]["discoveries"][label] += 1
                counts[appid][cohort][bucket].update(labels_seen)
            negative_aspects: set[str] = set()
            positive_aspects: set[str] = set()
            for value in item.get("a", []):
                label = label_for_aspect(value)
                if value["p"] in {"negative", "mixed"}:
                    negative_aspects.add(label)
                if value["p"] == "positive":
                    positive_aspects.add(label)
                if value.get("n"):
                    counts[appid][cohort]["discoveries"][label] += 1
            counts[appid][cohort]["aspects_negative"].update(negative_aspects)
            counts[appid][cohort]["aspects_positive"].update(positive_aspects)
            complaint_categories = {category(label) for label in counts_from_item(item, "co")}
            counts[appid][cohort]["categories"].update(complaint_categories)

    game_data: dict[str, Any] = {}
    for appid in APPIDS:
        game = manifest["games"][str(appid)]
        negative_total = valid[appid]["negative"]
        positive_total = valid[appid]["positive"]
        game_data[str(appid)] = {
            "name": GAME_NAMES[appid],
            "raw_reviews": game["raw_reviews"],
            "raw_negative": game["raw_negative"],
            "raw_positive": game["raw_positive"],
            "selected_negative": game["selected_negative"],
            "valid_negative": negative_total,
            "valid_positive": positive_total,
            "complaints": top_rows(counts[appid]["negative"]["complaints"], negative_total, 15),
            "complaint_examples": {
                label: [value[2] for value in sorted(by_app[appid], reverse=True)[:2]]
                for label, by_app in examples["negative"]["complaints"].items()
                if appid in by_app
            },
            "negative_aspects": top_rows(counts[appid]["negative"]["aspects_negative"], negative_total, 15),
            "feature_requests": top_rows(counts[appid]["negative"]["requests"], negative_total, 12),
            "technical_issues": top_rows(counts[appid]["negative"]["technical"], negative_total, 12),
            "positive_drivers": top_rows(counts[appid]["positive"]["praises"], positive_total, 12),
            "positive_aspects": top_rows(counts[appid]["positive"]["aspects_positive"], positive_total, 12),
            "complaint_categories": top_rows(counts[appid]["negative"]["categories"], negative_total, 8),
            "discoveries": top_rows(counts[appid]["negative"]["discoveries"], negative_total, 15),
            "sentiment": {cohort: dict(values) for cohort, values in sentiment[appid].items()},
        }

    shared: list[dict[str, Any]] = []
    labels = set(counts[APPIDS[0]]["negative"]["complaints"]) | set(
        counts[APPIDS[1]]["negative"]["complaints"]
    )
    for label in labels:
        game_counts = {appid: counts[appid]["negative"]["complaints"][label] for appid in APPIDS}
        if min(game_counts.values()) < 3:
            continue
        rates = {appid: rate(game_counts[appid], valid[appid]["negative"]) for appid in APPIDS}
        ranked_examples = {
            str(appid): [
                value[2]
                for value in sorted(
                    examples["negative"]["complaints"][label][appid], reverse=True
                )[:2]
            ]
            for appid in APPIDS
        }
        shared.append({
            "label": label,
            "name": display_label(label),
            "counts": {str(key): value for key, value in game_counts.items()},
            "rates": {str(key): value for key, value in rates.items()},
            "shared_score": min(rates.values()) + sum(rates.values()) / 2,
            "examples": ranked_examples,
        })
    shared.sort(key=lambda value: value["shared_score"], reverse=True)

    trend_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for appid, year, reviews, negative in trends:
        trend_data[str(appid)].append({
            "year": int(year), "reviews": int(reviews), "negative": int(negative),
            "negative_rate": rate(int(negative), int(reviews)),
        })

    return {
        "generated_at": manifest["created_at"],
        "model": cohort_results["negative"]["model"],
        "coverage": {
            "selected": manifest["selected_reviews"],
            "valid": sum(result["valid"] for result in cohort_results.values()),
            "negative_selected": manifest["cohorts"]["negative"]["reviews"],
            "negative_valid": cohort_results["negative"]["valid"],
            "positive_selected": manifest["cohorts"]["positive"]["reviews"],
            "positive_valid": cohort_results["positive"]["valid"],
            "reported_cost_usd": sum(result["reported_cost_usd"] for result in cohort_results.values()),
            "languages": len(manifest["cohorts"]["negative"]["languages"]),
        },
        "methodology": manifest["selection"],
        "games": game_data,
        "shared_complaints": shared[:15],
        "trends": dict(trend_data),
    }


def counts_from_item(item: dict[str, Any], key: str) -> set[str]:
    return {label_for_statement(value) for value in item.get(key, [])}


def render_html(data: dict[str, Any]) -> str:
    """Render after aggregate inspection; findings are filled from measured rates."""
    embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>What incremental players actually want</title>
<style>{REPORT_CSS}</style></head>
<body><main>
<header><div class=\"eyebrow\">STEAM REVIEW INTELLIGENCE · 16 AUG 2026</div>
<h1>What incremental players<br><em>actually want</em></h1>
<p class=\"lede\">Negative-first analysis of <strong>Rusty's Retirement</strong> and <strong>(the) Gnorp Apologue</strong>, with positive reviews used as a final control.</p>
<div id=\"heroStats\" class=\"stats\"></div></header>
<section><div class=\"section-title\"><span>01</span><h2>Executive findings</h2></div><div id=\"findings\" class=\"finding-grid\"></div></section>
<section><div class=\"section-title\"><span>02</span><h2>Shared complaint structure</h2></div><p class=\"dek\">Percentage of valid negative reviews mentioning each issue. Review-level rates prevent verbose reviews from dominating.</p><div id=\"sharedChart\" class=\"chart-card\"></div></section>
<section><div class=\"section-title\"><span>03</span><h2>Where the games diverge</h2></div><div id=\"gamePanels\" class=\"game-grid\"></div></section>
<section><div class=\"section-title\"><span>04</span><h2>Signal over time</h2></div><p class=\"dek\">Share of all collected reviews that were negative, by review year. The recent rise is descriptive—not proof of a product change or causal trend.</p><div id=\"trends\" class=\"game-grid\"></div></section>
<section><div class=\"section-title\"><span>05</span><h2>What players explicitly ask for</h2></div><p class=\"dek\">Direct requests are rare. Most needs are expressed as complaints, so these small rates are supporting evidence rather than the whole demand picture.</p><div id=\"requests\" class=\"game-grid\"></div></section>
<section><div class=\"section-title\"><span>06</span><h2>Positive reviews, last</h2></div><p class=\"dek\">The control cohort shows which parts of the promise already work—and therefore should not be designed away.</p><div id=\"positive\" class=\"game-grid\"></div></section>
<section><div class=\"section-title\"><span>07</span><h2>Design brief</h2></div><div id=\"brief\" class=\"brief\"></div></section>
<section><div class=\"section-title\"><span>08</span><h2>Method & limits</h2></div><div id=\"method\" class=\"method\"></div></section>
<footer>Generated from a frozen local Steam corpus · Structured with Gemini 3.7 Flash v2 · Raw review text was not embedded</footer>
</main><script>const DATA={embedded};{REPORT_JS}</script></body></html>"""


REPORT_CSS = r"""
:root{--ink:#18201d;--muted:#65706b;--paper:#f3f0e7;--card:#fffdf7;--rust:#dc6b3f;--gnorp:#6e8b73;--line:#d7d3c7;--gold:#d9a441}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}main{max-width:1240px;margin:auto;padding:42px 42px 80px}header{border-bottom:1px solid var(--line);padding:54px 0 42px}.eyebrow{letter-spacing:.18em;font-size:12px;font-weight:800;color:var(--rust)}h1{font-family:Georgia,serif;font-size:clamp(56px,8vw,108px);letter-spacing:-.065em;line-height:.84;margin:28px 0}h1 em{font-weight:400;color:var(--rust)}.lede{font-family:Georgia,serif;font-size:24px;line-height:1.45;max-width:850px;color:#424b47}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);margin-top:42px;border:1px solid var(--line)}.stat{background:var(--card);padding:24px}.stat b{display:block;font:700 34px Georgia,serif}.stat small{color:var(--muted);text-transform:uppercase;letter-spacing:.08em}section{padding:60px 0;border-bottom:1px solid var(--line)}.section-title{display:flex;align-items:baseline;gap:16px;margin-bottom:24px}.section-title span{color:var(--rust);font-weight:900}.section-title h2{font:700 38px Georgia,serif;margin:0}.dek{color:var(--muted);max-width:760px;line-height:1.6}.finding-grid,.game-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}.finding{background:var(--card);border:1px solid var(--line);padding:28px;min-height:220px}.finding .num{color:var(--rust);font-weight:900}.finding h3{font:700 25px Georgia,serif;margin:26px 0 12px}.finding p{color:var(--muted);line-height:1.55}.chart-card,.panel{background:var(--card);border:1px solid var(--line);padding:28px}.chart-row{display:grid;grid-template-columns:190px 1fr 52px;gap:12px;align-items:center;margin:13px 0}.chart-label{font-size:13px;font-weight:700}.track{height:12px;background:#e8e4d9;position:relative}.bar{height:100%;position:absolute;left:0;top:0}.rust{background:var(--rust)}.gnorp{background:var(--gnorp)}.value{text-align:right;font-variant-numeric:tabular-nums;font-size:12px}.legend{display:flex;gap:22px;margin-bottom:22px;color:var(--muted);font-size:13px}.dot{display:inline-block;width:9px;height:9px;margin-right:7px}.panel h3{font:700 27px Georgia,serif;margin:0 0 8px}.panel .meta{color:var(--muted);font-size:13px;margin-bottom:24px}.issue{margin:18px 0}.issue-head{display:flex;justify-content:space-between;gap:12px;font-size:13px;font-weight:750}.mini-track{height:7px;background:#e9e5da;margin-top:7px}.mini-bar{height:100%;background:var(--rust)}.quote{border-left:3px solid var(--gold);padding:4px 0 4px 14px;color:#59625e;font:italic 15px Georgia,serif;margin:18px 0}.brief{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.brief-card{padding:24px;background:var(--ink);color:white;min-height:220px}.brief-card b{color:#f2b15e;font-size:12px;letter-spacing:.12em}.brief-card h3{font:700 22px Georgia,serif}.brief-card p{color:#cad0cc;line-height:1.5}.method{background:#e8e4d9;padding:28px;display:grid;grid-template-columns:2fr 1fr;gap:30px;line-height:1.6}.method code{font-size:12px}footer{padding-top:32px;color:var(--muted);font-size:12px}@media(max-width:800px){main{padding:24px}.stats,.finding-grid,.game-grid,.brief,.method{grid-template-columns:1fr}.chart-row{grid-template-columns:110px 1fr 44px}h1{font-size:58px}}
.panel h3 a{color:inherit;text-decoration-thickness:1px;text-underline-offset:4px}.subhead{margin:26px 0 8px;padding-top:18px;border-top:1px solid var(--line);font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}.trend-row{display:grid;grid-template-columns:48px 1fr 58px;gap:10px;align-items:center;margin:12px 0;font-size:12px}.trend-track{height:18px;background:#e9e5da}.trend-bar{height:100%;min-width:2px;background:var(--gnorp)}
"""

REPORT_JS = r"""
const $=s=>document.querySelector(s),pct=v=>(v*100).toFixed(1)+'%';
const games=Object.entries(DATA.games), rust='2666510', gnorp='1473350';
$('#heroStats').innerHTML=[[DATA.coverage.valid.toLocaleString(),'reviews analyzed'],[pct(DATA.coverage.valid/DATA.coverage.selected),'structured coverage'],[DATA.coverage.languages,'languages'],['$'+DATA.coverage.reported_cost_usd.toFixed(2),'model cost']].map(x=>`<div class=stat><b>${x[0]}</b><small>${x[1]}</small></div>`).join('');
const shared=DATA.shared_complaints.slice(0,8);
$('#sharedChart').innerHTML=`<div class=legend><span><i class='dot rust'></i>Rusty's Retirement</span><span><i class='dot gnorp'></i>Gnorp Apologue</span></div>`+shared.map(x=>`<div class=chart-row><div class=chart-label>${x.name}</div><div class=track><div class='bar rust' style='width:${Math.min(100,x.rates[rust]*250)}%'></div></div><div class=value>${pct(x.rates[rust])}</div></div><div class=chart-row><div></div><div class=track><div class='bar gnorp' style='width:${Math.min(100,x.rates[gnorp]*250)}%'></div></div><div class=value>${pct(x.rates[gnorp])}</div></div>`).join('');
function issueList(rows,scale=250,limit=8){return rows.slice(0,limit).map(x=>`<div class=issue><div class=issue-head><span>${x.name}</span><span>${pct(x.rate)} · ${x.count}</span></div><div class=mini-track><div class=mini-bar style='width:${Math.min(100,x.rate*scale)}%'></div></div></div>`).join('')}
$('#gamePanels').innerHTML=games.map(([id,g])=>`<article class=panel><h3><a href='https://store.steampowered.com/app/${id}/' target=_blank rel=noopener>${g.name}</a></h3><div class=meta>${g.valid_negative} valid negative reviews · ${g.raw_reviews.toLocaleString()} total Steam reviews</div>${issueList(g.complaints)}<div class=subhead>Technical friction</div>${issueList(g.technical_issues,250,5)}</article>`).join('');
$('#trends').innerHTML=games.map(([id,g])=>`<article class=panel><h3>${g.name}</h3><div class=meta>Annual negative-review share; 2026 is a partial year</div>${DATA.trends[id].map(x=>`<div class=trend-row><b>${x.year}</b><div class=trend-track><div class=trend-bar style='width:${Math.min(100,x.negative_rate*500)}%'></div></div><span>${pct(x.negative_rate)}</span></div>`).join('')}</article>`).join('');
$('#requests').innerHTML=games.map(([id,g])=>`<article class=panel><h3>${g.name}</h3><div class=meta>Explicit requests inside negative reviews</div>${g.feature_requests.length?issueList(g.feature_requests,2500):'<p class=dek>Few explicit requests; needs are expressed mainly as complaints.</p>'}</article>`).join('');
$('#positive').innerHTML=games.map(([id,g])=>`<article class=panel><h3>${g.name}</h3><div class=meta>${g.valid_positive} sampled positive reviews</div>${issueList(g.positive_drivers,125)}</article>`).join('');
const find=(label,id)=>DATA.games[id].complaints.find(x=>x.label===label)?.rate||0;
const praise=(label,id)=>DATA.games[id].positive_drivers.find(x=>x.label===label)?.rate||0;
const findings=[
 ['Waiting needs decisions','Core-loop complaints reach '+pct(find('gameplay.core_loop',rust))+' in Rusty and '+pct(find('gameplay.core_loop',gnorp))+' in Gnorp. Players do not reject idling itself; they reject long stretches where observation replaces choice.'],
 ['Progression must branch','Progression is cited by '+pct(find('gameplay.progression',rust))+' and '+pct(find('gameplay.progression',gnorp))+' of negatives. Gnorp adds '+pct(find('gameplay.build_variety',gnorp))+' on build variety: upgrades need viable alternatives, not only larger numbers.'],
 ['The arc runs out too early','Content amount appears in '+pct(find('content.content_amount',rust))+' of Rusty negatives and '+pct(find('content.content_amount',gnorp))+' of Gnorp negatives. Endgame and replay complaints reinforce that the payoff needs to match the setup.'],
 ['Protect the proven fantasy','The core loop is also praised by '+pct(praise('gameplay.core_loop',rust))+' of sampled Rusty positives and '+pct(praise('gameplay.core_loop',gnorp))+' of Gnorp positives. The opportunity is more agency and depth—not replacing the idle fantasy.']
];
$('#findings').innerHTML=findings.map((x,i)=>`<article class=finding><span class=num>0${i+1}</span><h3>${x[0]}</h3><p>${x[1]}</p></article>`).join('');
$('#brief').innerHTML=[['PACE','Make waiting produce decisions','Idle time is accepted when it unlocks meaningful choices, visible milestones, or planning—not when it merely stretches the same loop.'],['DEPTH','Layer systems without obscuring them','Players want build variety, interactions, and alternate routes, supported by clear UI and reversible experimentation.'],['FINISH','Respect the late game','A satisfying incremental arc needs enough content, a deliberate endgame, and replay or continuation value proportional to the setup.']].map(x=>`<article class=brief-card><b>${x[0]}</b><h3>${x[1]}</h3><p>${x[2]}</p></article>`).join('');
$('#method').innerHTML=`<div><strong>Corpus.</strong> All substantive negative reviews (minimum 40 characters and four alphabetic tokens), then a deterministic 500-review positive sample per game. All languages were retained and normalized into English. Each topic counts at most once per review.<br><br><strong>Interpretation.</strong> Steam vote is not ground truth. Extracted statements are model-normalized, not verbatim quotations. Rates describe this frozen corpus, not all incremental players.</div><div><strong>Coverage</strong><br>${DATA.coverage.negative_valid}/${DATA.coverage.negative_selected} negative<br>${DATA.coverage.positive_valid}/${DATA.coverage.positive_selected} positive<br><br><strong>Model</strong><br>${DATA.model}<br><br><strong>Selection seed</strong><br><code>42</code></div>`;
"""


def main() -> None:
    data = aggregate()
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    REPORT_HTML.write_text(render_html(data))
    print(f"Wrote {REPORT_JSON} and {REPORT_HTML}")


if __name__ == "__main__":
    main()
