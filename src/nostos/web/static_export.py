"""Static HTML export: a single self-contained file with embedded CSS + JS.

The output file can be opened in any browser without a server. Filters are
client-side and operate on the embedded listing JSON. Action endpoints are
not available in the export (read-only); use the served `nostos web` for that.
"""

from __future__ import annotations

import html
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from nostos.web.query import SORT_OPTIONS, ListRow, to_jsonable

_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nostos listings — static export</title>
  <style>
{inline_css}
  </style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="#" onclick="return false">Nostos (export)</a>
    <span class="meta">profile: {profile_id} · {count} listing(s) · generated {generated_at}</span>
  </header>
  <main class="container">
    <section class="filter-card">
      <div class="filter-form">
        <div class="field">
          <label for="rent_min">Rent min</label>
          <input id="rent_min" type="number" step="50" min="0" placeholder="any">
        </div>
        <div class="field">
          <label for="rent_max">Rent max</label>
          <input id="rent_max" type="number" step="50" min="0" placeholder="any">
        </div>
        <div class="field">
          <label for="beds">Beds ≥</label>
          <input id="beds" type="number" step="1" min="0" placeholder="any">
        </div>
        <div class="field">
          <label for="baths_min">Baths ≥</label>
          <input id="baths_min" type="number" step="0.5" min="0" placeholder="any">
        </div>
        <div class="field">
          <label for="area_min">Area ≥</label>
          <input id="area_min" type="number" step="10" min="0" placeholder="any">
        </div>
        <div class="field">
          <label for="score_min">Score ≥</label>
          <input id="score_min" type="number" step="1" min="0" max="100" placeholder="any">
        </div>
        <div class="field">
          <label for="source">Source</label>
          <select id="source">
            <option value="">any</option>
          </select>
        </div>
        <div class="field">
          <label for="area">Area</label>
          <select id="area">
            <option value="">any</option>
          </select>
        </div>
        <div class="field">
          <label for="status">Status</label>
          <select id="status">
            <option value="">any</option>
            <option value="match">Match</option>
            <option value="unverified">Unverified</option>
            <option value="miss">Miss</option>
          </select>
        </div>
        <div class="field">
          <label for="sort">Sort</label>
          <select id="sort">
{sort_options}
          </select>
        </div>
        <div class="actions">
          <button type="button" id="reset">Reset</button>
        </div>
      </div>
      <p class="active-summary" id="summary"></p>
    </section>

    <section class="result-card">
      <table class="listings">
        <thead>
          <tr>
            <th>Photo</th><th>Score</th><th>Address</th><th>Source</th><th>Posted</th><th>Rent</th><th>Beds/Baths</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      <p class="empty" id="empty" hidden>No listings match the current filters.</p>
    </section>
  </main>

  <footer class="footer">
    <span>Static export · filters run in your browser ·
      use <code>nostos web</code> to record actions</span>
  </footer>

  <script id="data" type="application/json">{data_json}</script>
  <script>
(function() {{
  var dataNode = document.getElementById('data');
  var raw = JSON.parse(dataNode.textContent);
  var listings = raw.listings;
  var profileId = raw.profile_id;
  var sourceNode = document.getElementById('source');
  var sources = Array.from(new Set(listings.map(function(l) {{ return l.source; }}))).sort();
  sources.forEach(function(name) {{
    var opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    sourceNode.appendChild(opt);
  }});

  var areaNode = document.getElementById('area');
  var areaLabels = {{}};
  listings.forEach(function(l) {{
    if (l.area_key) areaLabels[l.area_key] = l.area_label || l.area_key;
  }});
  Object.keys(areaLabels).sort(function(a, b) {{
    return areaLabels[a].localeCompare(areaLabels[b]);
  }}).forEach(function(key) {{
    var opt = document.createElement('option');
    opt.value = key;
    opt.textContent = areaLabels[key];
    areaNode.appendChild(opt);
  }});

  var rows = document.getElementById('rows');
  var empty = document.getElementById('empty');
  var summary = document.getElementById('summary');

  function num(id) {{
    var v = document.getElementById(id).value;
    if (v === '' || v === null) return null;
    var n = Number(v);
    return Number.isFinite(n) ? n : null;
  }}

  function fmtPosted(iso) {{
    if (!iso) return '—';
    var d = new Date(iso);
    var diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return Math.max(0, Math.floor(diff)) + 's ago';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 2592000) return Math.floor(diff / 86400) + 'd ago';
    if (diff < 31536000) return Math.floor(diff / 2592000) + 'mo ago';
    return Math.floor(diff / 31536000) + 'y ago';
  }}

  function escape(s) {{
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }}

  function scoreBadge(s) {{
    if (s == null) return '<span class="score score-none">—</span>';
    var cls = s >= 75 ? 'score-good' : (s >= 50 ? 'score-mid' : 'score-low');
    return '<span class="score ' + cls + '">' + s.toFixed(1) + '</span>';
  }}

  function render() {{
    var rentMin = num('rent_min');
    var rentMax = num('rent_max');
    var beds = num('beds');
    var bathsMin = num('baths_min');
    var areaMin = num('area_min');
    var scoreMin = num('score_min');
    var source = document.getElementById('source').value;
    var area = document.getElementById('area').value;
    var status = document.getElementById('status').value;
    var sort = document.getElementById('sort').value;

    var filtered = listings.filter(function(l) {{
      if (rentMin != null && l.rent_value != null && l.rent_value < rentMin) return false;
      if (rentMax != null && l.rent_value != null && l.rent_value > rentMax) return false;
      if (beds != null && l.beds != null && l.beds < beds) return false;
      if (bathsMin != null && l.baths != null && l.baths < bathsMin) return false;
      if (areaMin != null && l.area_value != null && l.area_value < areaMin) return false;
      if (scoreMin != null && l.score < scoreMin) return false;
      if (source && l.source !== source) return false;
      if (area && l.area_key !== area) return false;
      if (status && l.match_status !== status) return false;
      return true;
    }});

    function byId(a, b) {{ return a.listing_id.localeCompare(b.listing_id); }}
    function numOr(v, fallback) {{ return v == null ? fallback : v; }}
    function postedTs(l) {{ return l.posted_at ? new Date(l.posted_at).getTime() : null; }}
    var sorters = {{
      score: function(a, b) {{ return b.score - a.score || byId(a, b); }},
      rent_asc: function(a, b) {{
        return numOr(a.rent_value, Infinity) - numOr(b.rent_value, Infinity) || byId(a, b);
      }},
      rent_desc: function(a, b) {{
        return numOr(b.rent_value, -Infinity) - numOr(a.rent_value, -Infinity) || byId(a, b);
      }},
      area_desc: function(a, b) {{
        return numOr(b.area_value, -Infinity) - numOr(a.area_value, -Infinity) || byId(a, b);
      }},
      posted_desc: function(a, b) {{
        return numOr(postedTs(b), -Infinity) - numOr(postedTs(a), -Infinity) || byId(a, b);
      }},
      posted_asc: function(a, b) {{
        return numOr(postedTs(a), Infinity) - numOr(postedTs(b), Infinity) || byId(a, b);
      }},
      address: function(a, b) {{
        return (a.address || '').localeCompare(b.address || '') || byId(a, b);
      }}
    }};
    filtered.sort(sorters[sort] || sorters.score);

    rows.innerHTML = filtered.map(function(l) {{
      var photo = l.primary_photo
        ? '<img class="thumb" src="' + escape(l.primary_photo) + '" loading="lazy" alt="">'
        : '<span class="thumb thumb-none">—</span>';
      var title = escape(l.title || l.address || l.listing_id);
      var addr = l.address ? '<div class="row-address">' + escape(l.address) + '</div>' : '';
      var posted = fmtPosted(l.posted_at);
      var bedsText = l.beds == null ? '—' : Math.floor(l.beds) + 'br';
      var bathsText = l.baths == null ? '—' : l.baths + 'ba';
      return '<tr>' +
        '<td>' + photo + '</td>' +
        '<td>' + scoreBadge(l.score) + '</td>' +
        '<td>' + title + addr + '</td>' +
        '<td><span class="source-tag source-' + escape(l.source) + '">' +
          escape(l.source) + '</span></td>' +
        '<td>' + posted + '</td>' +
        '<td>' + escape(l.rent_text) + '</td>' +
        '<td>' + bedsText + ' / ' + bathsText + '</td>' +
      '</tr>';
    }}).join('');

    empty.hidden = filtered.length > 0;
    summary.textContent =
      'Showing ' + filtered.length + ' of ' + listings.length +
      ' listing(s) (profile ' + escape(profileId) + ').';
  }}

  var ids = ['rent_min','rent_max','beds','baths_min','area_min',
              'score_min','source','area','status','sort'];
  ids.forEach(function(id) {{
    var el = document.getElementById(id);
    el.addEventListener('input', render);
    el.addEventListener('change', render);
  }});

  document.getElementById('reset').addEventListener('click', function() {{
    ['rent_min','rent_max','beds','baths_min','area_min','score_min'].forEach(function(id) {{
      document.getElementById(id).value = '';
    }});
    document.getElementById('source').value = '';
    document.getElementById('area').value = '';
    document.getElementById('status').value = '';
    document.getElementById('sort').value = 'score';
    render();
  }});

  render();
}})();
  </script>
</body>
</html>
"""


def render_static_export(
    rows: Iterable[ListRow],
    *,
    profile_id: str,
    inline_css: str,
) -> str:
    """Render the static export HTML for the given rows."""

    listings_json = [to_jsonable(row) for row in rows]
    payload = {
        "profile_id": profile_id,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "listings": listings_json,
    }
    data_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    sort_options = "\n".join(
        f'            <option value="{html.escape(key)}">{html.escape(label)}</option>'
        for key, label in SORT_OPTIONS
    )
    return _TEMPLATE.format(
        inline_css=_trim_css(inline_css),
        sort_options=sort_options,
        profile_id=html.escape(profile_id),
        count=len(listings_json),
        generated_at=html.escape(generated_at),
        data_json=html.escape(data_json),
    )


def write_static_export(
    rows: Iterable[ListRow],
    *,
    output_path: Path,
    profile_id: str,
) -> Path:
    """Render and write the static export to disk."""

    css_path = Path(__file__).resolve().parent / "static" / "style.css"
    inline_css = css_path.read_text(encoding="utf-8")
    body = render_static_export(rows, profile_id=profile_id, inline_css=inline_css)
    output_path.write_text(body, encoding="utf-8")
    return output_path


def _trim_css(css: str) -> str:
    """Minify the inline CSS a bit: strip leading whitespace per rule."""

    lines: list[str] = []
    for line in css.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


__all__ = ["render_static_export", "write_static_export"]
