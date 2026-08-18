#!/usr/bin/env python3
"""Aggregate the screenshot S/A-tier corpus and render a self-contained report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import duckdb

from steam_market.config import Settings


GAMES = {
    4304930: ("Chef Knight", "S"),
    3862670: ("Shelldiver", "S"),
    3767740: ("Outhold", "S"),
    3972320: ("Loot Loop", "S"),
    3948120: ("Scritchy Scratchy", "A"),
    4286550: ("Keep on Mining! - Worlds", "A"),
    3833760: ("You Know The Drill", "A"),
    3372980: ("Tower Wizard", "A"),
    4305480: ("IncreKnight", "A"),
}
DATA_DIR = Path("data/analysis/incremental-tier-list-2026-08-16")
REPORT_JSON = Path("reports/incremental-tier-list-analysis.json")
REPORT_HTML = Path("reports/incremental-tier-list-analysis.html")

LABEL_NAMES = {
    "gameplay.core_loop": "Core loop", "gameplay.pacing": "Pacing",
    "gameplay.progression": "Progression", "gameplay.replayability": "Replayability",
    "gameplay.balance": "Balance", "gameplay.difficulty": "Difficulty",
    "gameplay.controls": "Controls", "gameplay.level_design": "Run / level structure",
    "gameplay.build_variety": "Build variety", "gameplay.grind": "Grind",
    "gameplay.meta_progression": "Meta progression",
    "content.content_amount": "Amount of content", "content.content_variety": "Content variety",
    "content.endgame": "Endgame", "presentation.ui": "Interface",
    "presentation.ux": "User experience", "presentation.art_style": "Art style",
    "presentation.music": "Music", "presentation.audio": "Audio",
    "presentation.graphics": "Graphics", "presentation.animation": "Animation",
    "technical.performance": "Performance", "technical.bugs": "Bugs",
    "technical.crashes": "Crashes", "technical.save_system": "Save system",
    "technical.compatibility": "Compatibility", "product.price": "Price",
    "product.value": "Value", "product.updates": "Updates",
    "product.developer_support": "Developer support",
    "product.missing_features": "Missing features",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def statement_label(value: dict[str, Any]) -> str:
    label = value["l"]
    return f"{label}:{value['n']}" if value.get("n") else label


def display_label(label: str) -> str:
    if label in LABEL_NAMES:
        return LABEL_NAMES[label]
    base, _, novel = label.partition(":")
    return (novel or base.split(".", 1)[-1]).replace("_", " ").title()


def rate(count: int, total: int) -> float:
    return count / total if total else 0.0


def top_rows(counter: Counter[str], total: int, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {"label": label, "name": display_label(label), "count": count, "rate": rate(count, total)}
        for label, count in counter.most_common(limit)
    ]


def aggregate() -> dict[str, Any]:
    manifest = load_json(DATA_DIR / "manifest.json")
    results = {
        cohort: load_json(DATA_DIR / f"{cohort}-result.json")
        for cohort in ("negative", "positive")
    }
    ids = [recommendation_id for result in results.values() for recommendation_id in result["outputs"]]
    con = duckdb.connect(str(Settings().duckdb_path), read_only=True)
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = con.execute(
            f"SELECT recommendation_id,appid FROM reviews WHERE recommendation_id IN ({placeholders})",
            ids,
        ).fetchall()
    finally:
        con.close()
    app_for_id = {recommendation_id: appid for recommendation_id, appid in rows}

    buckets = ("complaints", "requests", "technical", "praises")
    counts = {
        appid: {cohort: {bucket: Counter() for bucket in buckets} for cohort in results}
        for appid in GAMES
    }
    valid = {appid: Counter() for appid in GAMES}
    source_keys = {"co": "complaints", "fr": "requests", "ti": "technical", "pr": "praises"}
    for cohort, result in results.items():
        for recommendation_id, item in result["outputs"].items():
            appid = app_for_id[recommendation_id]
            valid[appid][cohort] += 1
            for source_key, bucket in source_keys.items():
                counts[appid][cohort][bucket].update(
                    {statement_label(value) for value in item.get(source_key, [])}
                )

    games: dict[str, Any] = {}
    for appid, (name, tier) in GAMES.items():
        game_manifest = manifest["games"][str(appid)]
        negative_total = valid[appid]["negative"]
        positive_total = valid[appid]["positive"]
        games[str(appid)] = {
            "name": name, "tier": tier, "raw_reviews": game_manifest["raw_reviews"],
            "raw_negative": game_manifest["raw_negative"],
            "valid_negative": negative_total, "valid_positive": positive_total,
            "complaints": top_rows(counts[appid]["negative"]["complaints"], negative_total, 12),
            "feature_requests": top_rows(counts[appid]["negative"]["requests"], negative_total, 8),
            "technical_issues": top_rows(counts[appid]["negative"]["technical"], negative_total, 6),
            "positive_drivers": top_rows(counts[appid]["positive"]["praises"], positive_total, 10),
        }

    tiers: dict[str, Any] = {}
    tier_label_rates: dict[str, dict[str, float]] = {}
    for tier in ("S", "A"):
        tier_ids = [appid for appid, (_, game_tier) in GAMES.items() if game_tier == tier]
        labels = set().union(*(counts[appid]["negative"]["complaints"] for appid in tier_ids))
        rows_for_tier = []
        rates_for_tier = {}
        for label in labels:
            per_game = [rate(counts[appid]["negative"]["complaints"][label], valid[appid]["negative"]) for appid in tier_ids]
            average = sum(per_game) / len(tier_ids)
            rates_for_tier[label] = average
            rows_for_tier.append({
                "label": label, "name": display_label(label), "rate": average,
                "games": sum(value > 0 for value in per_game),
                "count": sum(counts[appid]["negative"]["complaints"][label] for appid in tier_ids),
            })
        rows_for_tier.sort(key=lambda row: row["rate"], reverse=True)
        tier_label_rates[tier] = rates_for_tier
        tiers[tier] = {
            "games": [str(appid) for appid in tier_ids],
            "valid_negative": sum(valid[appid]["negative"] for appid in tier_ids),
            "valid_positive": sum(valid[appid]["positive"] for appid in tier_ids),
            "complaints": rows_for_tier[:15],
        }

    comparison = []
    labels = set(tier_label_rates["S"]) | set(tier_label_rates["A"])
    for label in labels:
        s_rate = tier_label_rates["S"].get(label, 0.0)
        a_rate = tier_label_rates["A"].get(label, 0.0)
        comparison.append({
            "label": label, "name": display_label(label),
            "S": s_rate, "A": a_rate, "difference": s_rate - a_rate,
        })
    comparison.sort(key=lambda row: max(row["S"], row["A"]), reverse=True)

    return {
        "generated_at": manifest["created_at"], "model": results["negative"]["model"],
        "coverage": {
            "selected": manifest["selected_reviews"],
            "valid": sum(result["valid"] for result in results.values()),
            "negative_selected": manifest["cohorts"]["negative"]["reviews"],
            "negative_valid": results["negative"]["valid"],
            "positive_selected": manifest["cohorts"]["positive"]["reviews"],
            "positive_valid": results["positive"]["valid"],
            "languages": len(manifest["cohorts"]["negative"]["languages"]),
            "cost_usd": sum(result["reported_cost_usd"] for result in results.values()),
        },
        "methodology": manifest["selection"], "games": games,
        "game_order": [str(appid) for appid in GAMES], "tiers": tiers,
        "tier_comparison": comparison,
    }


def render_html(data: dict[str, Any]) -> str:
    embedded = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Inside the S and A tiers</title><style>{CSS}</style></head><body><main>
<header><div class=eyebrow>INCREMENTAL GAME REVIEW INTELLIGENCE · 16 AUG 2026</div><h1>Inside the<br><em>S & A tiers</em></h1><p class=lede>Nine acclaimed incremental games, analyzed negative-first. What separates the screenshot's S tier from A—and what players still want from both.</p><div id=stats class=stats></div></header>
<section><div class=title><span>01</span><h2>Executive conclusions</h2></div><p class=dek>The short version is not “players want bigger numbers.” They want stronger decisions, transformations, and payoff around an already attractive core fantasy.</p><div id=findings class=findings></div></section>
<section><div class=title><span>02</span><h2>S versus A complaint profile</h2></div><p class=dek>Equal-game-weighted share of substantive negative reviews. Each title has the same influence regardless of review volume.</p><div id=comparison class=card></div></section>
<section><div class=title><span>03</span><h2>What the problems actually mean</h2></div><p class=dek>Complaint labels become useful only after translating them into design implications and separating “more content” from “more waiting.”</p><div id=themes class=theme-grid></div></section>
<section><div class=title><span>04</span><h2>Nine game diagnoses</h2></div><p class=dek>Each title has a different failure mode. Rates use that game's own valid negative corpus; narrative diagnoses combine complaints with the positive control.</p><div id=games class=game-grid></div></section>
<section><div class=title><span>05</span><h2>Explicit requests</h2></div><p class=dek>Players usually state needs as complaints, so direct requests are a smaller supporting signal. They are shown as corroboration, not as a complete roadmap.</p><div id=requests class=game-grid></div></section>
<section><div class=title><span>06</span><h2>Positive reviews, last</h2></div><p class=dek>The control cohort identifies the fantasies and mechanics that should survive any redesign.</p><div id=positive class=game-grid></div></section>
<section><div class=title><span>07</span><h2>Opportunity principles</h2></div><div id=brief class=brief></div></section>
<section><div class=title><span>08</span><h2>Five game concepts worth testing</h2></div><p class=dek>These are not theme swaps. Each concept changes the structure of waiting, experimentation, or endgame to attack a measured genre weakness.</p><div id=concepts class=concepts></div></section>
<section><div class=title><span>09</span><h2>Method & limits</h2></div><div id=method class=method></div></section>
<footer>S/A placements transcribed from the supplied screenshot · Raw review text is not embedded</footer>
</main><script>const DATA={embedded};{JS}</script></body></html>"""


CSS = r"""
:root{--ink:#17201d;--muted:#66706c;--paper:#f1eee5;--card:#fffdf7;--s:#e25f3c;--a:#d1a43b;--green:#587b68;--line:#d7d2c5}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,system-ui,sans-serif}main{max-width:1280px;margin:auto;padding:40px 42px 80px}header{padding:55px 0 45px;border-bottom:1px solid var(--line)}.eyebrow{font-size:12px;font-weight:850;letter-spacing:.17em;color:var(--s)}h1{font:700 clamp(62px,9vw,112px)/.84 Georgia,serif;letter-spacing:-.065em;margin:28px 0}h1 em{font-weight:400;color:var(--s)}.lede{font:24px/1.45 Georgia,serif;color:#48514d;max-width:880px}.stats{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);margin-top:38px}.stat{padding:22px;background:var(--card);border-right:1px solid var(--line)}.stat:last-child{border:0}.stat b{display:block;font:700 32px Georgia,serif}.stat small{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}section{padding:56px 0;border-bottom:1px solid var(--line)}.title{display:flex;gap:15px;align-items:baseline;margin-bottom:22px}.title span{font-weight:900;color:var(--s)}h2{font:700 38px Georgia,serif;margin:0}.dek{color:var(--muted);line-height:1.55;max-width:820px}.findings{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.finding,.card,.game{background:var(--card);border:1px solid var(--line);padding:25px}.finding{min-height:190px}.finding b{color:var(--s);font-size:12px}.finding h3,.game h3{font:700 23px Georgia,serif;margin:20px 0 10px}.finding p{color:var(--muted);line-height:1.55}.legend{display:flex;gap:22px;color:var(--muted);font-size:12px;margin-bottom:20px}.dot{width:9px;height:9px;display:inline-block;margin-right:6px}.dot.s,.bar.s{background:var(--s)}.dot.a,.bar.a{background:var(--a)}.compare-row{display:grid;grid-template-columns:175px 1fr 50px;gap:10px;align-items:center;margin:10px 0}.label{font-size:12px;font-weight:750}.track{height:10px;background:#e9e4d8}.bar{height:100%}.value{text-align:right;font-size:11px;font-variant-numeric:tabular-nums}.game-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.game h3{margin:0 0 5px}.game h3 a{color:inherit;text-decoration-thickness:1px;text-underline-offset:3px}.meta{font-size:11px;color:var(--muted);margin-bottom:20px}.pill{display:inline-block;border-radius:20px;padding:3px 8px;color:white;font-size:10px;font-weight:900}.pill.S{background:var(--s)}.pill.A{background:var(--a);color:var(--ink)}.issue{margin:14px 0}.issue-head{display:flex;justify-content:space-between;gap:8px;font-size:11px;font-weight:700}.mini{height:6px;background:#e9e4d8;margin-top:5px}.mini div{height:100%;background:var(--green)}.subhead{border-top:1px solid var(--line);padding-top:14px;margin-top:18px;text-transform:uppercase;letter-spacing:.1em;font-size:10px;color:var(--muted)}.brief{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.brief article{background:var(--ink);color:white;padding:24px;min-height:210px}.brief b{color:#f4ae61;font-size:11px;letter-spacing:.12em}.brief h3{font:700 21px Georgia,serif}.brief p{color:#c9cfcc;line-height:1.5}.method{background:#e5e0d4;padding:26px;display:grid;grid-template-columns:2fr 1fr;gap:28px;line-height:1.55;font-size:13px}footer{padding-top:30px;color:var(--muted);font-size:11px}@media(max-width:850px){main{padding:24px}.stats,.findings,.game-grid,.brief,.method{grid-template-columns:1fr}.stat{border-right:0;border-bottom:1px solid var(--line)}.compare-row{grid-template-columns:110px 1fr 45px}h1{font-size:60px}}
.theme-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.theme{background:var(--card);border:1px solid var(--line);padding:26px}.theme .signal{font-size:11px;font-weight:850;letter-spacing:.09em;color:var(--s);text-transform:uppercase}.theme h3{font:700 23px Georgia,serif;margin:12px 0}.theme p{color:var(--muted);line-height:1.6;margin:0}.diagnosis{color:#48524d;font:15px/1.55 Georgia,serif;padding:14px 0 2px;border-bottom:1px solid var(--line);margin-bottom:18px}.concepts{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.concept{background:var(--card);border:1px solid var(--line);padding:28px}.concept:first-child{grid-column:1/-1;background:var(--ink);color:white}.concept .kicker{color:var(--s);font-size:11px;font-weight:900;letter-spacing:.12em}.concept:first-child .kicker{color:#f4ae61}.concept h3{font:700 26px Georgia,serif;margin:10px 0}.concept .pitch{font:17px/1.5 Georgia,serif;color:#4f5954}.concept:first-child .pitch{color:#d7ddda}.concept dl{display:grid;grid-template-columns:90px 1fr;gap:8px 12px;margin:20px 0 0;font-size:13px;line-height:1.5}.concept dt{font-weight:850;color:var(--s)}.concept:first-child dt{color:#f4ae61}.concept dd{margin:0;color:var(--muted)}.concept:first-child dd{color:#c9cfcc}.caution{margin-top:14px;padding:10px 12px;background:#f2ead8;color:#705d34;font-size:11px;line-height:1.45}@media(max-width:850px){.theme-grid,.concepts{grid-template-columns:1fr}.concept:first-child{grid-column:auto}}
"""


JS = r"""
const $=s=>document.querySelector(s),pct=v=>(v*100).toFixed(1)+'%';
const games=DATA.game_order.map(id=>[id,DATA.games[id]]),comp=DATA.tier_comparison.slice(0,10);
$('#stats').innerHTML=[[DATA.coverage.valid.toLocaleString(),'reviews analyzed'],[pct(DATA.coverage.valid/DATA.coverage.selected),'structured coverage'],['9','games · S and A'],['$'+DATA.coverage.cost_usd.toFixed(2),'model cost']].map(x=>`<div class=stat><b>${x[0]}</b><small>${x[1]}</small></div>`).join('');
function rows(items,scale=250,limit=7){return items.slice(0,limit).map(x=>`<div class=issue><div class=issue-head><span>${x.name}</span><span>${pct(x.rate)} · ${x.count}</span></div><div class=mini><div style='width:${Math.min(100,x.rate*scale)}%'></div></div></div>`).join('')}
$('#comparison').innerHTML=`<div class=legend><span><i class='dot s'></i>S tier, 4-game average</span><span><i class='dot a'></i>A tier, 5-game average</span></div>`+comp.map(x=>`<div class=compare-row><div class=label>${x.name}</div><div class=track><div class='bar s' style='width:${Math.min(100,x.S*300)}%'></div></div><div class=value>${pct(x.S)}</div></div><div class=compare-row><div></div><div class=track><div class='bar a' style='width:${Math.min(100,x.A*300)}%'></div></div><div class=value>${pct(x.A)}</div></div>`).join('');
const diagnoses={
 '4304930':'A compelling cooking-and-combat hook reaches its ceiling too early. Content dominates 62.0% of negatives, with value at 21.1%, while positives strongly endorse the core loop and art. The safest expansion is more recipes, encounters, and meaningful build branches—not slower unlocks.',
 '3862670':'The most polarized hook in the set: core-loop praise reaches 69.2%, yet 51.8% of negatives also attack the loop. Progression and content are the secondary fault lines. Add transformations and encounter variety before simply extending the same repeated action.',
 '3767740':'Outhold has a credible strategic foundation—build variety is a positive driver—but its negative arc combines limited content, grind, and progression friction. More stages alone would not solve it; new content must create alternative strategies rather than longer farming.',
 '3972320':'A compact, well-liked loop with an underbuilt second half. Nearly half of negatives cite content, and 21.3% cite meta progression. The opportunity is a stronger between-run layer, endgame goals, and item interactions that multiply replay value.',
 '3948120':'The strongest positive core-loop signal in the set at 75.0%, but also a visible repetition and quality burden: 34.8% core-loop complaints, 18.5% grind, and 13.9% bug reports. Polish and friction removal may create more value than adding another progression currency.',
 '4286550':'The clearest tuning problem in the sample. Progression, grind, pacing, and balance all exceed 29%. Players like mining and progression in positive reviews; the issue is how costs, waits, and upgrade choices deliver that fantasy. Shorter dead zones and fewer trap upgrades are the priority.',
 '3833760':'A promising mining fantasy with a polarizing moment-to-moment loop: 48.0% core-loop and 38.0% progression complaints. Because only 50 substantive negatives exist, treat exact rates cautiously, but test whether upgrades visibly change how drilling feels rather than only increasing output.',
 '3372980':'A high-value miniature that appears to stop while players are still engaged. Core-loop praise is 66.8% and value praise 24.2%, while negatives focus on the loop becoming repetitive and content ending. A deliberate postgame or remix mode fits better than padding the campaign.',
 '4305480':'The smallest negative sample, so direction matters more than exact rates. Content and progression lead, with combat, pacing, balance, and controls following. Preserve the well-liked progression fantasy, then improve combat agency and input feel before scaling the game outward.'
};
function gameCard(id,g,kind){const source=kind==='complaints'?g.complaints:kind==='requests'?g.feature_requests:g.positive_drivers;const scale=kind==='requests'?2200:kind==='positive'?130:250;return `<article class=game><span class='pill ${g.tier}'>${g.tier}</span><h3><a href='https://store.steampowered.com/app/${id}/' target=_blank rel=noopener>${g.name}</a></h3><div class=meta>${kind==='positive'?g.valid_positive+' sampled positives':g.valid_negative+' substantive negatives'}</div>${kind==='complaints'?`<p class=diagnosis>${diagnoses[id]}</p>`:''}${rows(source,scale)}${kind==='complaints'?`<div class=subhead>Technical friction</div>${rows(g.technical_issues,250,4)}`:''}</article>`}
$('#games').innerHTML=games.map(([id,g])=>gameCard(id,g,'complaints')).join('');
$('#requests').innerHTML=games.map(([id,g])=>gameCard(id,g,'requests')).join('');
$('#positive').innerHTML=games.map(([id,g])=>gameCard(id,g,'positive')).join('');
const common=DATA.tier_comparison.filter(x=>x.S>.04&&x.A>.04).sort((x,y)=>Math.min(y.S,y.A)-Math.min(x.S,x.A))[0];
const sGap=[...DATA.tier_comparison].sort((x,y)=>y.difference-x.difference)[0];
const aGap=[...DATA.tier_comparison].sort((x,y)=>x.difference-y.difference)[0];
const core=id=>DATA.games[id].positive_drivers.find(x=>x.label==='gameplay.core_loop')?.rate||0;
const avg=(ids,fn)=>ids.reduce((n,id)=>n+fn(id),0)/ids.length;
const sCore=avg(DATA.tiers.S.games,core),aCore=avg(DATA.tiers.A.games,core);
const tierRate=(label,tier)=>DATA.tier_comparison.find(x=>x.label===label)?.[tier]||0;
const findings=[
 ['The hook is not the tier separator','Core-loop complaints are almost identical: '+pct(common.S)+' in S and '+pct(common.A)+' in A. The supplied ranking appears to separate games by what happens around the hook—progression, pacing, and payoff—not by whether the first interaction is attractive.'],
 ['S-tier players mainly ask for continuation','Amount-of-content complaints reach '+pct(sGap.S)+' in S versus '+pct(sGap.A)+' in A. Chef Knight and Loot Loop are especially clear: players reach the ceiling while they still want to engage. Extensions should add decisions, not just time.'],
 ['A-tier games lose momentum during progression','A-tier progression complaints reach '+pct(tierRate('gameplay.progression','A'))+', pacing '+pct(tierRate('gameplay.pacing','A'))+', and grind '+pct(tierRate('gameplay.grind','A'))+'. The problem is the friction between meaningful transformations, not merely insufficient content.'],
 ['Praise and complaint can target the same loop','Core-loop praise averages '+pct(sCore)+' in S and '+pct(aCore)+' in A. Scritchy Scratchy reaches 75.0% positive-loop praise while 34.8% of its negatives still criticize repetition. A strong hook can delight many players and fatigue a minority.'],
 ['More hours can make the product worse','Grind is already cited by '+pct(tierRate('gameplay.grind','S'))+' of S and '+pct(tierRate('gameplay.grind','A'))+' of A negatives. Extending playtime through higher costs or slower resets would intensify an existing complaint rather than answer the request for content.'],
 ['Build freedom requires safe experimentation','A-tier balance complaints reach '+pct(tierRate('gameplay.balance','A'))+' versus '+pct(tierRate('gameplay.balance','S'))+' in S. Many choices do not feel like freedom when weak paths consume hours and recovery is expensive.'],
 ['Compact value is real—but creates a ceiling','Value complaints are higher in S ('+pct(tierRate('product.value','S'))+' versus '+pct(tierRate('product.value','A'))+'), while value remains a strong positive driver in games such as Tower Wizard. A game can be worth buying and still end before its systems mature.'],
 ['Treat small games as directional evidence','IncreKnight has 33 valid negatives and You Know The Drill has 50. Their patterns generate hypotheses, but exact percentages should not drive a roadmap without playtests, telemetry, and a larger post-update sample.']
];
$('#findings').innerHTML=findings.map((x,i)=>`<article class=finding><b>${String(i+1).padStart(2,'0')}</b><h3>${x[0]}</h3><p>${x[1]}</p></article>`).join('');
const themes=[
 ['41.9% S · 21.3% A','“More content” means more states, not more bars','Players are asking for new interactions, environments, build consequences, and goals. Reusing the same loop behind a larger number creates duration without discovery. A good expansion changes what the player thinks about every 20–40 minutes.'],
 ['32% in both tiers','The core loop must evolve before it exhausts itself','Repetition is inherent to incremental games, but sameness is optional. The input, visible output, strategic context, or automation layer should periodically transform while preserving the original fantasy.'],
 ['27.8% progression · 17.7% pacing in A','Progress is felt through changed agency','A percentage increase is legible but emotionally weak. Players feel progress when a former bottleneck disappears, a manual task becomes programmable, or a new system changes priorities.'],
 ['16.8% grind · 12.5% balance in A','A choice is not meaningful if experimentation is punitive','Long recovery from a weak build converts curiosity into risk avoidance. Forecasts, respecs, parallel loadouts, and fast failure loops let balance complexity create stories instead of regret.'],
 ['High praise and content complaints coexist','A finite ending and an endless mode solve different jobs','An authored ending gives closure; an endless or remix layer gives attachment somewhere to go. Trying to make one mode do both often produces either an abrupt stop or an overlong final grind.'],
 ['Direct requests usually below 6%','Complaint language is the stronger discovery source','Players rarely write a product specification. “Boring,” “slow,” or “too short” must be translated into an underlying need, then validated with prototypes rather than implemented literally.']
];
$('#themes').innerHTML=themes.map(x=>`<article class=theme><div class=signal>${x[0]}</div><h3>${x[1]}</h3><p>${x[2]}</p></article>`).join('');
$('#brief').innerHTML=[
 ['DECISIONS','Turn waiting into planning','Idle time should accumulate options, forecasts, or queued decisions—not merely postpone the next click.'],
 ['TRANSFORM','Make upgrades alter play','Regularly retire old bottlenecks, automate solved work, and introduce a new strategic question.'],
 ['EXPERIMENT','Make builds reversible','Show expected effects, support parallel loadouts, and keep recovery from a bad idea short.'],
 ['BREADTH','Multiply content through systems','Prefer interacting mechanics and emergent combinations over hundreds of linear upgrade nodes.'],
 ['CLOSURE','Design an actual ending','Deliver a clear climax and payoff before inviting players into prestige, remix, or endless play.'],
 ['CLARITY','Expose the useful math','Surface bottlenecks, time-to-goal, and why output changed; hide arithmetic that does not support a decision.']
].map(x=>`<article><b>${x[0]}</b><h3>${x[1]}</h3><p>${x[2]}</p></article>`).join('');
const concepts=[
 {n:'01',name:'Clockwork Commons',pitch:'A town-sized incremental where the player designs rules for autonomous workers instead of repeatedly clicking their jobs.',loop:'Observe bottlenecks, write simple if/then schedules, then watch a short simulated day execute.',idle:'Offline time produces a replayable activity log and three decision points; the game never silently spends rare resources.',progression:'New districts introduce constraints—weather, transport, worker needs—that recombine existing systems rather than only multiplying output.',proof:'Directly addresses passive waiting, hidden math, and the desire for content created through interacting systems.',risk:'Automation must remain readable. Limit rules early and visualize why every worker chose an action.'},
 {n:'02',name:'The Last Expedition',pitch:'A finite 10-hour incremental campaign with a real ending, followed by a remix mode that changes world rules rather than resetting numbers.',loop:'Prepare an expedition, choose risks and supplies, then let it travel while managing the base and reacting to discoveries.',idle:'Time away advances safe travel; high-risk decisions wait for the player instead of resolving invisibly.',progression:'Each region removes one old constraint and introduces a new planning layer. Completion unlocks weekly seeded worlds with modifiers.',proof:'Answers the S-tier content ceiling while separating authored closure from replayability and avoiding an endless final grind.',risk:'The campaign must advertise its finite scope so the ending feels intentional rather than like missing content.'},
 {n:'03',name:'Branchforge',pitch:'An incremental crafting roguelite built around reversible build experiments and side-by-side outcome forecasts.',loop:'Assemble a machine from modules, run a five-minute production challenge, inspect the bottleneck, and revise.',idle:'Saved blueprints continue at a capped baseline; returning players choose which unexpected result to exploit.',progression:'Unlock interactions and rule-breaking modules, not flat tiers. Keep three parallel builds and compare projected time-to-goal.',proof:'Targets balance, grind, and progression complaints by making failure cheap and build variety genuinely usable.',risk:'Forecasts should explain direction, not solve the entire optimization puzzle for the player.'},
 {n:'04',name:'Deep Ecology',pitch:'A biological incremental where every prestige creates a persistent ecosystem, so resets add relationships rather than erase progress.',loop:'Introduce species, tune habitats, and harvest resources while managing predator, climate, and mutation feedback.',idle:'The ecosystem simulates offline within player-set guardrails and pauses before irreversible collapse.',progression:'Old worlds become resource-producing biomes that interact with new ones; the metagame is composing a network of ecosystems.',proof:'Creates combinatorial content and visible transformation without stretching the same collection loop.',risk:'Prevent runaway opacity with causal overlays that show which relationship created each gain or collapse.'},
 {n:'05',name:'Guild of Small Heroes',pitch:'A cozy combat incremental where solved battles become programmable tactics and every prestige adds a new hero role.',loop:'Play a battle actively once, convert the successful approach into an automation script, then focus on the next unsolved encounter.',idle:'The guild repeats proven missions offline; uncertain missions queue tactical questions for the next session.',progression:'Horizontal party synergies replace a single damage ladder. Respecs are free between expeditions and failed experiments retain discoveries.',proof:'Preserves the satisfying active hook while ensuring progress reduces repetition and expands agency.',risk:'Automation must feel earned but arrive before mastery turns into chores.'}
];
$('#concepts').innerHTML=concepts.map(x=>`<article class=concept><div class=kicker>CONCEPT ${x.n}</div><h3>${x.name}</h3><p class=pitch>${x.pitch}</p><dl><dt>Core loop</dt><dd>${x.loop}</dd><dt>Idle design</dt><dd>${x.idle}</dd><dt>Progression</dt><dd>${x.progression}</dd><dt>Evidence fit</dt><dd>${x.proof}</dd></dl><div class=caution><strong>Design risk:</strong> ${x.risk}</div></article>`).join('');
$('#method').innerHTML=`<div><strong>Corpus.</strong> Every negative review with at least 40 characters and four alphabetic tokens, plus a deterministic sample of up to 500 eligible positive reviews per game. All languages were retained and normalized into English. Topics count once per review.<br><br><strong>Comparison.</strong> Tier rates are averages of per-game rates, so each game contributes equally. The screenshot ranking is one player's tier list, not an objective outcome variable. Steam votes and model-normalized statements have uncertainty; these rates describe a frozen corpus.</div><div><strong>Coverage</strong><br>${DATA.coverage.negative_valid}/${DATA.coverage.negative_selected} negative<br>${DATA.coverage.positive_valid}/${DATA.coverage.positive_selected} positive<br><br><strong>Languages</strong><br>${DATA.coverage.languages} in negative corpus<br><br><strong>Model</strong><br>${DATA.model}<br><br><strong>Seed</strong><br>42</div>`;
"""


def main() -> None:
    data = aggregate()
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    REPORT_HTML.write_text(render_html(data))
    print(f"Wrote {REPORT_JSON} and {REPORT_HTML}")


if __name__ == "__main__":
    main()
