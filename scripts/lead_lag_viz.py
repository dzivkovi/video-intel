#!/usr/bin/env python3
"""Self-contained interactive HTML for the lead-lag findings (issue #94).

Renders the coverage-corrected lead-lag data (issue #93, scripts/lead_lag_report.py)
as ONE self-contained HTML file: a "who leads the conversation" ranked view and a
concept adoption-chain timeline. No external CDNs; all CSS/JS inline; the only
outbound links are the evidence deep links to YouTube.

Design system mirrors the existing Idea Map artifact (dark ground, gold accent,
serif display, mono eyebrows). Statistics come straight from lead_lag_report's
build_report_data - this module renders, it does not recompute.

Codex gate constraint (PR #96): the ranking view must not present a small-sample
leader (expected firsts < 2) without foregrounding observed/expected counts; the
most robust leader (highest lift with expected >= 5) is called out explicitly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from intel_graph import timestamped_url
from lead_lag_report import (
    DEFAULT_DB,
    MIN_RANKED_CONCEPTS_DEFAULT,
    TOP_FINDINGS_DEFAULT,
    Chain,
    ReportData,
    build_report_data,
    extract_quote,
    finding_chains,
    ranked_creators,
    spearman,
)

if TYPE_CHECKING:
    pass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("lead_lag_viz")

SMALL_SAMPLE_EXPECTED = 2.0  # below this, a lift is one lucky first away from noise
ROBUST_EXPECTED = 5.0  # at or above this, the lift is backed by enough trials to trust


def _clean_text(text: str) -> str:
    """Whitespace-flatten and dash-normalize corpus strings (same rule as extract_quote)."""
    return " ".join(text.split()).replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def _chain_payload(chain: Chain, rankable: frozenset[str]) -> dict:
    mentions = []
    for m in chain.mentions:
        link = timestamped_url(m.url, m.start_seconds if "youtube.com" in m.url else None)
        mentions.append(
            {
                "creator": m.source_id,
                "date": m.first_date.isoformat(),
                "title": _clean_text(m.title),
                "link": link,
                "quote": extract_quote(m.segment_text, m.as_mentioned, width=180) if m.segment_text else None,
                "subThreshold": m.source_id not in rankable,
            }
        )
    return {
        "concept": chain.concept_id,
        "mentions": mentions,
        "spanDays": (chain.mentions[-1].first_date - chain.mentions[0].first_date).days,
        "followers": len(chain.edges),
    }


def build_viz_payload(
    data: ReportData,
    top_findings: int = TOP_FINDINGS_DEFAULT,
    min_ranked_concepts: int = MIN_RANKED_CONCEPTS_DEFAULT,
) -> dict:
    """The JSON the page embeds. Mirrors render_report's selection logic exactly."""
    ranked = ranked_creators(data, min_ranked_concepts)
    naive_order = [src for src, _ in sorted(data.naive.items(), key=lambda kv: kv[1], reverse=True)]
    leaders = []
    for s in ranked:
        c = data.coverage[s.source_id]
        leaders.append(
            {
                "creator": s.source_id,
                "lift": round(s.lift, 2),
                "firsts": round(s.firsts, 1),
                "expected": round(s.expected, 1),
                "eligible": s.eligible_concepts,
                "smallSample": s.expected < SMALL_SAMPLE_EXPECTED,
                "robust": s.expected >= ROBUST_EXPECTED,
                "naiveRank": naive_order.index(s.source_id) + 1 if s.source_id in naive_order else None,
                "artifacts": c.n_artifacts,
            }
        )
    # select on the UNROUNDED lift: a 2-dp rounded 1.0 must not fail the > 1.0
    # gate that the ranking order itself treats as above baseline (PR #97 review)
    robust_stats = [s for s in ranked if s.expected >= ROBUST_EXPECTED and s.lift > 1.0]
    most_robust = max(robust_stats, key=lambda s: s.lift).source_id if robust_stats else None
    # the biggest ranked channel's naive-vs-corrected movement is what the
    # kill-rule card cites; derived from data, never hardcoded (PR #97 review)
    kill_rule = None
    if ranked:
        biggest = max(ranked, key=lambda s: data.coverage[s.source_id].n_artifacts)
        kill_rule = {
            "creator": biggest.source_id,
            "artifacts": data.coverage[biggest.source_id].n_artifacts,
            "naiveRank": (naive_order.index(biggest.source_id) + 1 if biggest.source_id in naive_order else None),
            "correctedRank": ranked.index(biggest) + 1,
            "rankedCount": len(ranked),
        }

    if len(ranked) >= 2:
        lifts = [s.lift for s in ranked]
        rho_start = round(spearman(lifts, [float(data.coverage[s.source_id].start.toordinal()) for s in ranked]), 2)
        rho_size = round(spearman(lifts, [float(data.coverage[s.source_id].n_artifacts) for s in ranked]), 2)
    else:
        rho_start = rho_size = None

    chains = [_chain_payload(c, data.rankable) for c in finding_chains(data, top_findings)]
    return {
        "generated": dt.date.today().isoformat(),
        "stats": {
            "artifacts": sum(c.n_artifacts for c in data.coverage.values()),
            "creators": len(data.coverage),
            "rankable": len(data.rankable),
            "concepts": data.n_concepts_total,
            "eligibleConcepts": data.n_concepts_eligible,
        },
        "params": {
            **data.params,
            "min_ranked_concepts": min_ranked_concepts,
            "top": top_findings,
            "small_sample_expected": SMALL_SAMPLE_EXPECTED,
            "robust_expected": ROBUST_EXPECTED,
        },
        "leaders": leaders,
        "omittedRanked": len(data.rankable) - len(ranked),
        "mostRobust": most_robust,
        "killRule": kill_rule,
        "diagnostics": {"rhoStart": rho_start, "rhoSize": rho_size},
        "chains": chains,
    }


def render_html(payload: dict) -> str:
    """One self-contained page.

    ALL angle brackets in the JSON are unicode-escaped, not just "</": a
    corpus string containing "<!--" plus "<script" flips the HTML tokenizer
    into the script-data double-escaped state, where the template's real
    closing tag no longer ends the script element and the whole page renders
    blank (adversarial review on PR #97, reproduced in-browser). YouTube
    titles are attacker-adjacent input; escape the class, not the instance.
    """
    data_json = (
        json.dumps(payload, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    # order matters: fill the static sentinel FIRST so corpus strings inside the
    # JSON can never contain-and-get-mutated-by a template placeholder (Codex review)
    return _TEMPLATE.replace("__GENERATED__", payload["generated"]).replace("__DATA_JSON__", data_json)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Who Leads the AI-Coding Conversation</title>
<link rel="icon" href="data:,">
<style>
  :root{
    --ground:#0F1320; --ground2:#12172A; --panel:#1A2032; --panel2:#212B44;
    --line:#2B3552; --line2:#39456A; --text:#EAE6DA; --muted:#A7AEC4; --faint:#7D86A3;
    --accent:#E4A64C; --accent-dim:#8A6E38; --good:#83BF9A; --miss:#D08A66;
    --serif:'Iowan Old Style','Palatino Linotype',Palatino,'Book Antiqua',Georgia,serif;
    --sans:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,'SF Mono','Cascadia Mono',Menlo,Consolas,monospace;
  }
  html,body{margin:0;padding:0;}
  body{background:var(--ground);color:var(--text);font-family:var(--sans);line-height:1.55;
    font-size:15px;-webkit-font-smoothing:antialiased;padding:0 0 64px;overflow-x:hidden;}
  *{box-sizing:border-box;}
  .wrap{max-width:1180px;margin:0 auto;padding:0 24px;}
  a{color:var(--accent);text-decoration:none;} a:hover{text-decoration:underline;}
  h1,h2,h3{text-wrap:balance;font-family:var(--serif);font-weight:600;letter-spacing:.01em;}
  .eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--accent);margin:0 0 10px;}
  .lead{color:var(--muted);max-width:62ch;}
  .num{font-variant-numeric:tabular-nums;}
  header.hero{padding:44px 0 26px;border-bottom:1px solid var(--line);
    background:radial-gradient(120% 90% at 15% -10%, #1a2238 0%, transparent 60%), var(--ground);}
  header.hero h1{font-size:40px;line-height:1.05;margin:0 0 14px;}
  .statrow{display:flex;flex-wrap:wrap;gap:26px;margin-top:24px;}
  .stat{display:flex;flex-direction:column;gap:2px;}
  .stat .v{font-family:var(--serif);font-size:26px;color:var(--text);}
  .stat .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);}
  section{padding:40px 0 8px;}
  h2.stitle{font-size:24px;margin:0 0 8px;}
  .sdesc{color:var(--muted);max-width:64ch;margin:0 0 22px;}

  /* leader bars */
  .callout{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--good);
    border-radius:11px;padding:13px 16px;margin:0 0 20px;max-width:640px;font-size:14px;color:#cfcabb;}
  .callout b{color:var(--good);}
  .bars{display:flex;flex-direction:column;gap:9px;max-width:760px;}
  .lbar{display:grid;grid-template-columns:150px 1fr 210px;align-items:center;gap:12px;}
  .lbar .lbl{font-family:var(--mono);font-size:12.5px;color:var(--text);text-align:right;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .lbar .track{height:13px;background:var(--panel2);border-radius:999px;overflow:hidden;position:relative;}
  .lbar .fill{height:100%;background:linear-gradient(90deg,var(--accent-dim),var(--accent));border-radius:999px;}
  .lbar.small .fill{background:repeating-linear-gradient(45deg,var(--accent-dim),var(--accent-dim) 5px,#5c4b28 5px,#5c4b28 10px);opacity:.75;}
  .lbar.robust .fill{background:linear-gradient(90deg,#5f8f72,var(--good));}
  .lbar .meta{font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap;}
  .lbar .meta b{color:var(--text);font-weight:600;}
  .badge{display:inline-block;font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;
    padding:1px 7px;border-radius:999px;margin-left:6px;vertical-align:1px;}
  .badge.small{background:rgba(208,138,102,.13);color:var(--miss);border:1px solid rgba(208,138,102,.32);}
  .badge.robust{background:rgba(131,191,154,.15);color:var(--good);border:1px solid rgba(131,191,154,.35);}
  .baseline{position:absolute;top:-3px;bottom:-3px;width:1px;background:var(--faint);}
  .legend{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:14px;max-width:760px;}
  .tier{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--muted);border-bottom:1px dashed var(--line);padding-bottom:4px;margin:14px 0 2px;}
  .tier:first-child{margin-top:0;}
  .tier.good{color:var(--good);} .tier.warn{color:var(--miss);}

  /* diagnostics */
  .diaggrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;max-width:900px;align-items:start;}
  .diag{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:16px 18px;}
  .diag .v{font-family:var(--serif);font-size:30px;}
  .diag .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:4px;}
  .diag p{font-size:12.5px;color:var(--muted);margin:8px 0 0;}
  .diag .v.ok{color:var(--good);} .diag .v.warn{color:var(--miss);}

  /* timeline */
  .tlshell{display:grid;grid-template-columns:270px 1fr;gap:18px;align-items:start;}
  .tlist{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;max-height:480px;overflow-y:auto;
    scrollbar-width:thin;scrollbar-color:var(--line2) transparent;}
  .tlist::after{content:"";position:sticky;bottom:-12px;display:block;height:34px;margin:-34px -12px -12px;
    background:linear-gradient(180deg,transparent,var(--panel));pointer-events:none;}
  .tcount{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
    color:var(--muted);padding:2px 11px 8px;border-bottom:1px dashed var(--line);margin-bottom:6px;}
  .titem{display:block;width:100%;text-align:left;background:none;border:0;border-radius:9px;color:var(--text);
    font-family:var(--mono);font-size:12px;padding:9px 11px;cursor:pointer;line-height:1.4;}
  .titem:hover{background:var(--panel2);}
  .titem.active{background:var(--panel2);outline:1px solid var(--line2);}
  .titem .cnt{color:var(--faint);display:block;font-size:10.5px;}
  .tlbox{background:radial-gradient(130% 120% at 50% 0%, #141b32 0%, var(--ground2) 70%);
    border:1px solid var(--line);border-radius:14px;padding:18px;overflow-x:auto;min-height:420px;}
  .tlbox svg{display:block;width:100%;height:auto;}
  .dot{cursor:pointer;}
  .dot circle{fill:var(--accent);stroke:#0F1320;stroke-width:2;}
  .dot.sub circle{fill:var(--faint);}
  .dot.leader circle{fill:var(--good);}
  .dot text{fill:var(--text);font-family:var(--mono);font-size:11px;
    paint-order:stroke;stroke:#12172A;stroke-width:3.5px;stroke-linejoin:round;}
  .dot .d{fill:var(--muted);font-size:10px;}
  .axis line{stroke:var(--line);} .axis text{fill:var(--muted);font-family:var(--mono);font-size:10px;}
  .flow{stroke:var(--accent-dim);stroke-width:1.5;fill:none;marker-end:url(#arr);opacity:.55;}
  #tip{position:fixed;pointer-events:none;background:rgba(15,19,32,.96);border:1px solid var(--line2);
    border-radius:9px;padding:10px 12px;max-width:340px;font-size:12.5px;color:var(--text);z-index:9;display:none;}
  #tip .q{font-family:var(--serif);font-style:italic;color:#d8d3c4;margin-top:6px;}
  #tip .t{font-family:var(--mono);font-size:10.5px;color:var(--muted);}
  .tlhint{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:10px;}

  .explain{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:26px 28px;}
  .explain p{max-width:70ch;color:#cfcabb;margin:0 0 14px;}
  .explain p:last-child{margin-bottom:0;}
  .explain b{color:var(--text);}
  @media(max-width:860px){ .tlshell{grid-template-columns:1fr;} .lbar{grid-template-columns:110px 1fr;}
    .lbar .meta{grid-column:2;} header.hero h1{font-size:30px;} }

  /* print (#101): the screen design above is reviewed and locked - everything
     print-specific lives in .print-only (hidden on screen) and this block */
  .print-only{display:none;}
  @page{size:Letter;margin:12mm 10mm;}
  @media print{
    :root{--ground:#fff;--ground2:#fff;--panel:#fff;--panel2:#ececec;--line:#bbb;--line2:#999;
      --text:#111;--muted:#444;--faint:#666;--accent:#7a5a1e;--accent-dim:#a5854a;--good:#2e6b47;--miss:#8a4a28;}
    *{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
    body{background:#fff;color:#111;padding:0;font-size:12px;}
    header.hero{background:#fff;padding:12px 0 10px;}
    header.hero h1{font-size:26px;}
    section{padding:14px 0 4px;break-inside:avoid;}
    .callout,.diag,.explain,.lbar{break-inside:avoid;}
    .lbar .fill{background:#a5854a;} .lbar.robust .fill{background:#2e6b47;}
    .tlshell,.tlhint,#tip{display:none !important;}
    .print-only{display:block;}
    .pchain{break-inside:avoid;border-bottom:1px solid #bbb;padding:8px 0 10px;}
    .pchain h3{font-size:14px;margin:0 0 6px;}
    .pchain ol{margin:0;padding-left:20px;}
    .pchain li{margin:0 0 6px;}
    .pchain .pq{font-family:var(--serif);font-style:italic;color:#333;}
    .pchain .purl{font-family:var(--mono);font-size:9.5px;color:#555;word-break:break-all;}
  }
</style>
</head>
<body>
<div id="tip"></div>
<header class="hero"><div class="wrap">
  <p class="eyebrow">Video-Intel &middot; lead-lag layer &middot; issues #93 / #94 &middot; generated __GENERATED__</p>
  <h1>Who Leads the AI-Coding Conversation</h1>
  <p class="lead">Every concept in the corpus, traced to who covered it first and who followed - corrected for
  how far back each channel's coverage goes, and for how much each channel posts. Being loud is not leading;
  this is the view with the volume turned off.</p>
  <div class="statrow" id="stats"></div>
</div></header>

<section><div class="wrap">
  <p class="eyebrow">The ranking</p>
  <h2 class="stitle">Precursor lift: firsts earned vs firsts expected</h2>
  <p class="sdesc">A lift of 1.0 means a channel is first exactly as often as its posting volume predicts.
  Above 1.0 it leads beyond its volume; below, it follows. Hatched bars are small samples - one or two lucky
  firsts away from noise. Green is the robust tier (enough expected firsts to trust).</p>
  <div class="callout" id="callout"></div>
  <div class="bars" id="bars"></div>
  <p class="legend" id="legend"></p>
</div></section>

<section><div class="wrap">
  <p class="eyebrow">Kill-criterion check</p>
  <h2 class="stitle">Is this just popularity again?</h2>
  <p class="sdesc">Issue #93's kill rule: if the corrected leaders were just the biggest or oldest-indexed
  channels, there is no signal. Rank correlation says otherwise.</p>
  <div class="diaggrid" id="diag"></div>
</div></section>

<section><div class="wrap">
  <p class="eyebrow">Adoption chains</p>
  <h2 class="stitle">Watch a concept travel across creators</h2>
  <p class="sdesc">Pick a concept. Each dot is a creator's first mention, in time order; green is the chain
  leader, grey dots (where a chain has one) are channels too small to rank - they still count as
  observations. Click a dot to open the video at the cited timestamp.</p>
  <div class="tlshell">
    <div class="tlist" id="tlist"></div>
    <div>
      <div class="tlbox"><svg id="tl" role="img" aria-label="adoption chain timeline"></svg></div>
      <p class="tlhint">hover a dot for the evidence quote &middot; click to open the video at the timestamp &middot;
      only the horizontal axis carries meaning - vertical position is reading order</p>
    </div>
  </div>
</div></section>

<section class="print-only"><div class="wrap">
  <p class="eyebrow">Adoption chains</p>
  <h2 class="stitle">All concept adoption chains</h2>
  <p class="sdesc">Print has no interactivity, so every chain the interactive timeline offers renders here in full,
  with the evidence link URL for each first mention.</p>
  <div id="printchains"></div>
</div></section>

<section><div class="wrap">
  <p class="eyebrow">Plain english</p>
  <h2 class="stitle">How to read this honestly</h2>
  <div class="explain" id="explain"></div>
</div></section>

<script>
const DATA = __DATA_JSON__;

function el(tag, cls, html){const e=document.createElement(tag);if(cls)e.className=cls;if(html!==undefined)e.innerHTML=html;return e;}
function esc(s){return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

/* hero stats */
(function(){
  const s = DATA.stats, host = document.getElementById('stats');
  const items = [[s.artifacts,'videos'],[s.creators,'channels'],[s.rankable,'rankable'],[s.concepts,'concepts'],[s.eligibleConcepts,'chains analyzed']];
  for(const [v,k] of items){const d=el('div','stat');d.append(el('div','v num',esc(v)),el('div','k',esc(k)));host.append(d);}
})();

/* leader bars - tiered so the top visual slot goes to evidence, not luck */
(function(){
  const host = document.getElementById('bars');
  const L = DATA.leaders;
  if(!L.length){host.append(el('p','sdesc','No creators clear the ranking thresholds.'));return;}
  const maxLift = Math.max(...L.map(x=>x.lift), 1.5);
  const tiers = [
    {cls:'good', label:'robust tier - enough trials to trust', rows:L.filter(x=>x.robust)},
    {cls:'', label:'mid tier - suggestive, thin evidence', rows:L.filter(x=>!x.robust && !x.smallSample)},
    {cls:'warn', label:'small sample - leads to verify, not verdicts', rows:L.filter(x=>x.smallSample)},
  ];
  for(const tier of tiers){
    if(!tier.rows.length) continue;
    host.append(el('div','tier '+tier.cls, esc(tier.label)));
    for(const x of tier.rows){
      const row = el('div','lbar'+(x.smallSample?' small':'')+(x.robust&&x.lift>1?' robust':''));
      const badge = x.smallSample ? '<span class="badge small">small sample</span>'
                  : (x.robust ? '<span class="badge robust">robust</span>' : '');
      row.append(el('div','lbl',esc(x.creator)));
      const track = el('div','track');
      const fill = el('div','fill'); fill.style.width = Math.max(2,(x.lift/maxLift)*100).toFixed(1)+'%';
      track.append(fill);
      const base = el('div','baseline'); base.style.left = ((1.0/maxLift)*100).toFixed(1)+'%'; base.title='lift = 1.0 (volume-expected)';
      track.append(base);
      row.append(track);
      const naive = x.naiveRank ? ' &middot; naive #'+x.naiveRank : '';
      row.append(el('div','meta','<b>'+x.lift.toFixed(2)+'</b> lift &middot; '+x.firsts+' obs / '+x.expected+' exp'+naive+badge));
      host.append(row);
    }
  }
  const legend = document.getElementById('legend');
  const omitted = DATA.omittedRanked ? ' '+DATA.omittedRanked+' rankable creators omitted for fewer than '+DATA.params.min_ranked_concepts+' eligible concepts.' : '';
  legend.textContent = 'Bars sorted by lift within each evidence tier; bar length is comparable across tiers. Vertical line = lift 1.0 (leading exactly as much as posting volume predicts). obs/exp = observed vs volume-expected firsts over that creator\'s eligible concepts; naive #N = rank in the uncorrected first-mention count.' + omitted;
  const cal = document.getElementById('callout');
  if(DATA.mostRobust){
    const r = L.find(x=>x.creator===DATA.mostRobust);
    cal.innerHTML = 'Most robust leader: <b>'+esc(r.creator)+'</b> - lift '+r.lift.toFixed(2)+' backed by '+r.firsts+' observed firsts against '+r.expected+' expected over '+r.eligible+' concepts. Larger lifts in the small-sample tier ride on one or two lucky firsts; this row is the one the data actually supports.';
  } else { cal.style.display='none'; }
})();

/* diagnostics */
(function(){
  const host = document.getElementById('diag');
  const d = DATA.diagnostics;
  function card(k, v, interp, ok){
    const c = el('div','diag');
    c.append(el('div','k',k));
    c.append(el('div','v num'+(ok?' ok':' warn'), v===null?'n/a':(v>0?'+':'')+v.toFixed(2)));
    const p = el('p'); p.textContent = interp; c.append(p);
    host.append(c);
  }
  card('Spearman: lift vs corpus size', d.rhoSize,
    d.rhoSize===null ? 'not enough ranked creators' :
    (d.rhoSize <= 0 ? 'Negative or zero: bigger channels do not rank higher after correction. If popularity were driving the ranking this would be strongly positive.' :
     'Positive: bigger channels still rank higher - popularity contamination.'), d.rhoSize!==null && d.rhoSize <= 0);
  card('Spearman: lift vs coverage start', d.rhoStart,
    d.rhoStart===null ? 'not enough ranked creators' :
    (Math.abs(d.rhoStart) < 0.35 ? 'Weak: indexing age is not driving the ranking. (Negative direction = older-indexed slightly favored; watched, not fatal.)' :
     'Strong: indexing depth still drives the ranking - coverage artifact.'), d.rhoStart!==null && Math.abs(d.rhoStart) < 0.35);
  const c3 = el('div','diag');
  c3.append(el('div','k','The kill rule'));
  let verdict = 'Issue #93: if, after coverage correction, the leaders are just the biggest / oldest-indexed channels, the influence signal is not there.';
  const kr = DATA.killRule;
  if(kr){
    verdict += ' On this data: the largest ranked channel, '+esc(kr.creator)+' ('+kr.artifacts+' videos'+(kr.naiveRank?', naive #'+kr.naiveRank:'')+'), lands at corrected #'+kr.correctedRank+' of '+kr.rankedCount+'.';
    verdict += (d.rhoSize!==null && d.rhoSize <= 0 && Math.abs(d.rhoStart) < 0.35 && kr.correctedRank > kr.rankedCount/2)
      ? ' The criterion is not met - the signal survives.'
      : ' Re-examine before trusting the ranking: a size or indexing-age artifact tracks rank on this regeneration.';
  }
  c3.append(el('p','', verdict));
  host.append(c3);
})();

/* timeline */
(function(){
  const list = document.getElementById('tlist');
  const svg = document.getElementById('tl');
  const tip = document.getElementById('tip');
  const NS = 'http://www.w3.org/2000/svg';
  let active = 0;

  function fmt(d){return d.slice(2);}
  function draw(idx){
    active = idx;
    [...list.querySelectorAll('.titem')].forEach((b,i)=>b.classList.toggle('active', i===idx));
    const chain = DATA.chains[idx];
    const M = chain.mentions;
    // scale row height so short chains still fill the panel instead of
    // clustering in the top third (fresh-eye review finding)
    const W = 900, rowH = Math.max(46, Math.min(86, Math.floor(340/M.length))), padL = 20, padR = 200, padT = 42, padB = 16;
    const H = padT + M.length*rowH + padB;
    svg.setAttribute('viewBox','0 0 '+W+' '+H);
    svg.innerHTML = '<defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#8A6E38"/></marker></defs>';
    const t0 = new Date(M[0].date).getTime(), t1 = new Date(M[M.length-1].date).getTime();
    const span = Math.max(t1 - t0, 864e5);
    const x = t => padL + 60 + ((t - t0)/span) * (W - padL - padR - 60);
    /* axis */
    const ax = document.createElementNS(NS,'g'); ax.setAttribute('class','axis');
    const axLine = document.createElementNS(NS,'line');
    axLine.setAttribute('x1',padL+60); axLine.setAttribute('x2',W-padR);
    axLine.setAttribute('y1',padT-16); axLine.setAttribute('y2',padT-16);
    ax.append(axLine);
    for(const [t,anchor] of [[t0,'start'],[t1,'end']]){
      const tx = document.createElementNS(NS,'text');
      tx.setAttribute('x', x(t)); tx.setAttribute('y', padT-24); tx.setAttribute('text-anchor', anchor);
      tx.textContent = new Date(t).toISOString().slice(0,10);
      ax.append(tx);
    }
    svg.append(ax);
    /* flow line through dots */
    let dpath = '';
    M.forEach((m,i)=>{ const px = x(new Date(m.date).getTime()), py = padT + i*rowH + 14;
      dpath += (i? ' L':'M')+px.toFixed(1)+','+py.toFixed(1); });
    if(M.length>1){ const p = document.createElementNS(NS,'path'); p.setAttribute('d',dpath); p.setAttribute('class','flow'); svg.append(p); }
    /* dots */
    M.forEach((m,i)=>{
      const g = document.createElementNS(NS,'g');
      g.setAttribute('class','dot'+(i===0?' leader':'')+(m.subThreshold?' sub':''));
      const px = x(new Date(m.date).getTime()), py = padT + i*rowH + 14;
      const c = document.createElementNS(NS,'circle');
      c.setAttribute('cx',px); c.setAttribute('cy',py); c.setAttribute('r',7);
      g.append(c);
      const lab = document.createElementNS(NS,'text');
      lab.setAttribute('x',px+13); lab.setAttribute('y',py+4);
      lab.textContent = m.creator + (m.subThreshold?' (small)':'');
      g.append(lab);
      const dte = document.createElementNS(NS,'text');
      dte.setAttribute('x',px+13); dte.setAttribute('y',py+17); dte.setAttribute('class','d');
      dte.textContent = fmt(m.date);
      g.append(dte);
      g.addEventListener('click',()=>{ if(m.link) window.open(m.link,'_blank','noopener'); });
      g.addEventListener('mousemove',(ev)=>{
        tip.style.display='block';
        tip.style.left = Math.min(ev.clientX+14, window.innerWidth-360)+'px';
        tip.style.top = (ev.clientY+12)+'px';
        tip.innerHTML = '<div class="t">'+esc(m.creator)+' &middot; '+esc(m.date)+'</div><div>'+esc(m.title)+'</div>'+(m.quote?'<div class="q">&ldquo;'+esc(m.quote)+'&rdquo;</div>':'');
      });
      g.addEventListener('mouseleave',()=>{ tip.style.display='none'; });
      svg.append(g);
    });
  }
  list.append(el('div','tcount', DATA.chains.length+' concepts &middot; scroll'));
  DATA.chains.forEach((c,i)=>{
    const b = el('button','titem');
    b.innerHTML = esc(c.concept.split('.').pop().replace(/_/g,' ')) +
      '<span class="cnt">'+c.mentions.length+' creators &middot; '+c.spanDays+' days &middot; leads: '+esc(c.mentions[0].creator)+'</span>';
    b.addEventListener('click',()=>draw(i));
    list.append(b);
  });
  if(DATA.chains.length) draw(0); else document.querySelector('.tlbox').textContent = 'No chains with follow edges.';
})();

/* print chains (#101): all chains expanded sequentially, URLs visible */
(function(){
  const host = document.getElementById('printchains');
  DATA.chains.forEach(c => {
    const box = el('div','pchain');
    box.append(el('h3','', esc(c.concept)+' &middot; '+c.mentions.length+' adopters over '+c.spanDays+' days'));
    const ol = el('ol');
    const first = c.mentions[0] ? c.mentions[0].date : '';
    c.mentions.forEach((m,i) => {
      const lag = i===0 ? 'leader' : '+' + Math.round((new Date(m.date)-new Date(first))/86400000) + 'd';
      const li = el('li');
      let html = '<b>'+esc(m.creator)+'</b>'+(m.subThreshold?' (below ranking threshold)':'')+' &middot; '+esc(m.date)+' &middot; '+lag+'<br>'+esc(m.title);
      if(m.quote) html += '<br><span class="pq">&ldquo;'+esc(m.quote)+'&rdquo;</span>';
      html += '<br><span class="purl">'+esc(m.link)+'</span>';
      li.innerHTML = html;
      ol.append(li);
    });
    box.append(ol);
    host.append(box);
  });
})();

/* explain */
(function(){
  const p = DATA.params;
  document.getElementById('explain').innerHTML =
    '<p><b>What was corrected.</b> Channels entered this corpus with very different lookback depths, so a deep-backfill channel would look "first" on anything that emerged before the others were indexed. A concept only counts here when at least '+p.min_eligible+' of its adopters were already being indexed at emergence, and expected firsts are proportional to posting rate - so a channel only scores by leading beyond its volume-implied chance.</p>' +
    '<p><b>What the hatching means.</b> A hatched bar has fewer than '+DATA.params.small_sample_expected+' expected firsts: one or two lucky calls produce a huge lift. Read those rows as leads to verify, not verdicts. The green rows (>= '+DATA.params.robust_expected+' expected) have enough trials to take at face value.</p>' +
    '<p><b>What this is not.</b> Not a subscriber ranking, not a view-count ranking, and not causal proof that one creator watched another. It is precedence in this corpus, on '+DATA.stats.eligibleConcepts+' concepts that clear the filters, over '+DATA.stats.artifacts+' videos. Upload date stands in for idea date; anything said earlier off-platform is invisible.</p>' +
    '<p><b>Where the numbers come from.</b> scripts/lead_lag_report.py (issue #93), read-only over the DuckDB truth store; this page renders the same statistics without recomputing them. Method: minimal form of the coverage correction in "Precursors and Laggards" (arXiv:1009.0119).</p>';
})();
</script>
</body>
</html>
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Self-contained lead-lag HTML visualization (issue #94)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"DuckDB path (default {DEFAULT_DB})")
    parser.add_argument("--out", help="write HTML here (default: stdout)")
    parser.add_argument("--top", type=int, default=TOP_FINDINGS_DEFAULT, help="number of chains to include")
    parser.add_argument(
        "--pdf",
        help="also print-to-PDF here via headless Chromium (optional dependency: "
        "pip install playwright && playwright install chromium); requires --out",
    )
    return parser


def render_pdf(html_path: Path, pdf_path: Path) -> None:
    """Print the generated page to PDF with headless Chromium (issue #101).

    Lazy import: playwright is an optional dependency, same pattern as the
    repo's other extras. printBackground keeps the print stylesheet's
    palette; the @media print rules handle black-on-white and expansion.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(html_path.resolve().as_uri())
            page.wait_for_load_state("load")  # self-contained file:// page; networkidle is the wrong primitive here
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
            )
        finally:
            browser.close()
    log.info("PDF written to %s", pdf_path)


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        import duckdb
    except ImportError:
        log.error("duckdb not installed. Run: pip install 'video-intel[intelligence]'")
        sys.exit(1)
    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DuckDB store not found at %s. Build it first: python scripts/intel_graph.py load", db_path)
        sys.exit(1)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        data = build_report_data(con)
    finally:
        con.close()
    if args.pdf and not args.out:
        log.error("--pdf requires --out (the PDF is printed from the written HTML file)")
        sys.exit(1)
    html = render_html(build_viz_payload(data, top_findings=args.top))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        log.info("Visualization written to %s", out)
        if args.pdf:
            render_pdf(out, Path(args.pdf))
    else:
        print(html)


if __name__ == "__main__":
    main()
