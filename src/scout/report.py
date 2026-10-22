"""The daily brief as one self-contained HTML page: two tabs, Video ideas and Dev intel.

No script, font or image is loaded (a Content-Security-Policy says so); every value is escaped by Jinja's
autoescape; a link is shown only when it is http(s) and came from a stored item, never from the model's text.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment

_TEMPLATE = r"""{% macro evidence(items) -%}
{% if items %}<div class="label">Evidence</div><ul class="evidence">
{% for e in items %}<li>{% if e.url %}<a href="{{ e.url }}" target="_blank" rel="noopener noreferrer">{{ e.label }}</a>{% else %}{{ e.label }}{% endif %}
<span class="src"> · {{ e.where }} · {{ e.stats }}</span></li>{% endfor %}</ul>{% endif %}
{%- endmacro %}
{% macro raw(rows) -%}
<article class="card"><h2>Top items (no summary today)</h2>
{% for e in rows %}<div class="row">{% if e.url %}<a href="{{ e.url }}" target="_blank" rel="noopener noreferrer">{{ e.label }}</a>{% else %}{{ e.label }}{% endif %}
<div class="src">{{ e.where }} · {{ e.stats }}</div></div>{% else %}<div class="row">Nothing passed the floors today.</div>{% endfor %}
</article>
{%- endmacro %}
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name="referrer" content="no-referrer">
<title>Scout brief {{ day }}</title>
<style>
:root { --bg:#f6f5f2; --card:#ffffff; --ink:#1c1b19; --muted:#6b675f; --line:#e3e0d8; --accent:#9a3412;
  --chip:#f0ede6; --good:#166534; --warn:#a16207; --bad:#b91c1c; --new:#1d4ed8; }
@media (prefers-color-scheme: dark) { :root { --bg:#131211; --card:#1d1c1a; --ink:#ecebe7; --muted:#a29e95;
  --line:#2f2d2a; --accent:#fb923c; --chip:#2a2825; --good:#4ade80; --warn:#facc15; --bad:#f87171; --new:#93c5fd; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
  Roboto, sans-serif; }
main { max-width:980px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; }
.meta { color:var(--muted); font-size:13px; margin:0 0 16px; }
.meta b { color:var(--ink); font-weight:600; }
.warnings { border-left:3px solid var(--warn); padding:8px 12px; background:var(--card); margin:0 0 16px; font-size:13px; }
.tabs > input { position:absolute; opacity:0; pointer-events:none; }
.tabs > label { display:inline-block; padding:8px 14px; margin:0 6px 12px 0; border:1px solid var(--line);
  border-radius:999px; cursor:pointer; font-weight:600; background:var(--card); }
.tabs > input:checked + label { background:var(--ink); color:var(--bg); border-color:var(--ink); }
.tabs > input:focus-visible + label { outline:2px solid var(--accent); outline-offset:2px; }
.panel { display:none; }
#t-ideas:checked ~ #p-ideas, #t-dev:checked ~ #p-dev { display:block; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin:0 0 12px; }
.card h2 { font-size:17px; margin:0 0 6px; line-height:1.35; }
.thesis { font-style:italic; margin:0 0 8px; }
.label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; margin:10px 0 2px; }
ul { margin:4px 0 0; padding-left:20px; }
li { margin:2px 0; }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 0; }
.chip { background:var(--chip); border-radius:6px; padding:2px 8px; font-size:12px; color:var(--muted); }
.chip b { color:var(--ink); font-weight:600; }
.chip.low { color:var(--good); } .chip.medium { color:var(--warn); } .chip.high { color:var(--bad); }
.chip.new { color:var(--new); font-weight:600; }
.kind { color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }
.evidence a, .row a { color:var(--accent); text-decoration:none; overflow-wrap:anywhere; }
.evidence a:hover, .row a:hover { text-decoration:underline; }
.evidence li { font-size:13px; }
.src { color:var(--muted); font-size:12px; }
.row { border-top:1px solid var(--line); padding:8px 0; font-size:14px; }
.row:first-child { border-top:0; }
.demand { columns:2 220px; font-size:13px; color:var(--muted); }
footer { color:var(--muted); font-size:12px; margin-top:24px; }
</style>
</head>
<body>
<main>
<h1>Scout brief · {{ day }}{% if profile %} · {{ profile }}{% endif %}</h1>
<p class="meta">ScrapeCreators credits: <b>{{ credits.spent }}</b> of {{ credits.cap }} today
{%- if credits.balance is not none %} · <b>{{ credits.balance }}</b> left{% endif %}
{%- if credits.external %} · {{ credits.external }} used elsewhere{% endif %}
 · {{ counts.total }} items from {{ counts.sources }} · {{ made_with }}</p>
{% if warnings %}<div class="warnings">{% for w in warnings %}<div>{{ w }}</div>{% endfor %}</div>{% endif %}
<div class="tabs">
<input type="radio" name="tab" id="t-ideas" checked><label for="t-ideas">Video ideas ({{ ideas|length or top.video_ideas|length }})</label>
<input type="radio" name="tab" id="t-dev"><label for="t-dev">Dev intel ({{ dev|length or top.dev_intel|length }})</label>

<section class="panel" id="p-ideas">
{% for card in ideas %}
<article class="card">
<h2>{{ card.title }}</h2>
<p class="thesis">{{ card.thesis }}</p>
<div>{{ card.why_now }}</div>
{% if card.hooks %}<div class="label">Hooks</div><ul>{% for h in card.hooks %}<li>{{ h }}</li>{% endfor %}</ul>{% endif %}
{% if card.questions %}<div class="label">Viewers ask</div><ul>{% for q in card.questions %}<li>{{ q }}</li>{% endfor %}</ul>{% endif %}
<div class="chips">
<span class="chip">score <b>{{ card.final }}</b></span>
<span class="chip">worth <b>{{ card.worthiness }}</b></span>
<span class="chip {{ card.policy_risk }}">policy risk {{ card.policy_risk }}</span>
{% for w in card.wiki %}<span class="chip">Wikipedia {{ w.title }}: z <b>{{ w.z }}</b> ({{ w.recent_daily }}/day)</span>{% endfor %}
</div>
{{ evidence(card.evidence_items) }}
</article>
{% else %}
{{ raw(top.video_ideas) }}
{% endfor %}
{% if demand %}<article class="card"><h2>What people type into YouTube</h2><div class="demand">
{% for line in demand %}<div>{{ line }}</div>{% endfor %}</div></article>{% endif %}
</section>

<section class="panel" id="p-dev">
{% for card in dev %}
<article class="card">
<div class="kind">{{ card.kind|replace("_", " ") }} · {{ card.stage }}</div>
<h2>{{ card.title }}</h2>
<div>{{ card.summary }}</div>
{% if card.steps %}<div class="label">Steps</div><ol>{% for s in card.steps %}<li>{{ s }}</li>{% endfor %}</ol>{% endif %}
<div class="label">Action</div><div>{{ card.action }}</div>
<div class="chips">
<span class="chip">score <b>{{ card.final }}</b></span>
{% for t in card.tools %}<span class="chip{% if t.new %} new{% endif %}">{% if t.new %}NEW {% endif %}{{ t.name }} · {{ t.stance }}</span>{% endfor %}
</div>
{{ evidence(card.evidence_items) }}
</article>
{% else %}
{{ raw(top.dev_intel) }}
{% endfor %}
</section>
</div>
<footer>Made by <code>scout daily</code>. Posts are evidence, never a script: nothing here goes on screen.
Signals: engagement over age (velocity), its percentile in its source, debate in the votes, YouTube views over the
channel's median (outlier), extra sources carrying the same link. {{ footer_note }}</footer>
</main>
</body>
</html>
"""


def safe_url(url: str | None) -> str | None:
    """``url`` when it is http(s), else None (no ``javascript:`` or ``data:`` link can reach the page)."""
    if url and url.lower().startswith(("https://", "http://")):
        return url
    return None


def render_brief(context: dict[str, Any]) -> str:
    environment = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return environment.from_string(_TEMPLATE).render(**context)
