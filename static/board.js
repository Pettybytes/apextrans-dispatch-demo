/* ApexTrans Dispatch Control - board logic.
   Read-only. Polls the existing apex-dispatch endpoints every 3s and derives
   everything else in the browser. No emoji on this page. No secrets: nothing
   here reads keys, and nothing is written back to the service. */

'use strict';

(function () {
  var POLL_MS = 3000;
  var LIMIT = 500;          // the auto-approval limit, shown on the cost chart
  var SPEED_MPH = 55;       // planning speed, matches the rules engine
  var SVGNS = 'http://www.w3.org/2000/svg';

  var state = {
    exceptions: [],
    proposals: [],
    driversById: {},        // merged across polls, so a driver that goes On Load keeps its name
    updates: [],
    audit: [],
    shipments: {},          // shipment_id -> full record (terminals, margin)
    squads: null,
    ok: true
  };

  var ORDER = { waiting: 0, open: 1, approved: 2, recovered: 3, blocked: 4 };

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(n) { return (Number(n) || 0).toLocaleString('en-US'); }
  function money(n) { return '$' + num(Math.round(Number(n) || 0)); }
  function shortId(id) { return String(id || '').replace(/^APX-/, ''); }
  function hhmm(iso) { return String(iso || '').slice(11, 16); }

  function svgEl(tag, attrs, text) {
    var el = document.createElementNS(SVGNS, tag);
    for (var k in attrs) { if (Object.prototype.hasOwnProperty.call(attrs, k)) el.setAttribute(k, attrs[k]); }
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
  }

  function jget(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(url + ' -> ' + r.status);
      return r.json();
    });
  }

  /* ------------------------------------------------------------- fetching */
  async function ensureShipments() {
    var need = state.exceptions.map(function (e) { return e.shipment_id; })
      .filter(function (id) { return !state.shipments[id]; });
    if (!need.length) return;
    var got = await Promise.all(need.map(function (id) {
      return jget('/shipments/' + encodeURIComponent(id)).catch(function () { return null; });
    }));
    got.forEach(function (s, i) { if (s) state.shipments[need[i]] = s; });
  }

  async function tick() {
    try {
      var res = await Promise.all([
        jget('/exceptions'),
        jget('/reroutes'),
        jget('/drivers/available'),
        jget('/updates'),
        jget('/audit')
      ]);
      state.exceptions = res[0].exceptions || [];
      state.proposals = res[1].proposals || [];
      (res[2].drivers || []).forEach(function (d) {
        state.driversById[d.driver_id] = Object.assign({}, state.driversById[d.driver_id], d);
      });
      state.updates = res[3].updates || [];
      state.audit = res[4].audit || [];
      // an applied driver leaves the available list, so keep the name from the feed
      state.updates.forEach(function (u) {
        if (u.driver_id) {
          state.driversById[u.driver_id] = Object.assign({}, state.driversById[u.driver_id], { name: u.driver_name });
        }
      });
      await ensureShipments();
      state.ok = true;
    } catch (err) {
      state.ok = false;
      console.error('board: poll failed', err);
    }
    render();
  }

  /* ------------------------------------------------------------- deriving */
  function latestProposal(sid) {
    for (var i = 0; i < state.proposals.length; i++) {
      if (state.proposals[i].shipment_id === sid) return state.proposals[i];
    }
    return null;
  }

  function statusOf(p) {
    if (!p) return 'open';
    if (p.decision === 'auto_applied') return 'recovered';
    if (p.decision === 'pending_human' && p.status === 'applied') return 'approved';
    if (p.decision === 'pending_human') return 'waiting';
    return 'open';
  }

  var PILL = {
    waiting: 'Waiting for a dispatcher',
    recovered: 'Recovered automatically',
    approved: 'Approved by a dispatcher',
    open: 'Open',
    blocked: 'Blocked by Kong'
  };

  function driverName(id) {
    var d = id ? state.driversById[id] : null;
    return d && d.name ? d.name : null;
  }

  function plainWhy(conflicts) {
    if (!conflicts || !conflicts.length) {
      return 'Cleared every dispatch rule and stayed under the $500 approval limit.';
    }
    return conflicts.map(function (c) {
      var m;
      if ((m = c.match(/^HOS shortfall: reroute needs ([\d.]+)h .* has ([\d.]+)h/))) {
        return 'Driver hours too short: ' + m[1] + ' h needed, ' + m[2] + ' h left';
      }
      if ((m = c.match(/^cost \$(\d+) over \$(\d+) limit/))) {
        return 'Estimated cost ' + money(m[1]) + ' is over the ' + money(m[2]) + ' approval limit';
      }
      if ((m = c.match(/^equipment mismatch: (.*) load needs (.*), driver has (.*)$/))) {
        return 'Equipment mismatch: the load needs a ' + m[2] + ', the driver has a ' + m[3];
      }
      if (/^Hazmat load requires Hazmat endorsement/.test(c)) return 'Hazmat load needs a Hazmat-endorsed driver';
      if (/^Reefer load requires Reefer-certified/.test(c)) return 'Reefer load needs a Reefer-certified driver';
      if ((m = c.match(/^(\S+) is not Available \(status=(.*)\)/))) return 'That driver is not available (' + m[2] + ')';
      if (/already holds a pending proposal/.test(c)) return 'That driver is already on another proposal';
      if (/still misses delivery due/.test(c)) return 'The new ETA still misses the delivery window';
      if (/Critical priority/.test(c)) return 'Critical load: a person has to sign off';
      return c;
    }).join(' · ');
  }

  /* The HOS clock that actually failed, read straight off the conflict text. */
  function hosClock(p) {
    if (!p) return '';
    var blob = (p.conflicts || []).join(' | ');
    var out = [];
    var m;
    if ((m = blob.match(/Drive limit: needs ([\d.]+) h, driver has ([\d.]+) h left today/))) {
      out.push('drive ' + m[2] + ' h left of ' + m[1] + ' h needed');
    }
    if ((m = blob.match(/On-duty window: 14-hour window closes in ([\d.]+) h/))) {
      out.push('14-hour window closes in ' + m[1] + ' h');
    }
    if ((m = blob.match(/Weekly cycle: ([\d.]+) of 70 hours used/))) {
      out.push('cycle ' + m[1] + ' of 70 h');
    }
    return out.length ? 'HOS clock: ' + out.join(' · ') : '';
  }

  function hoursFor(p, e) {
    var hit = null;
    (p.conflicts || []).some(function (c) {
      var m = c.match(/Drive limit: needs ([\d.]+) h, driver has ([\d.]+) h left today/);
      if (m) { hit = { needed: Number(m[1]), left: Number(m[2]) }; return true; }
      return false;
    });
    if (hit) return hit;
    var d = state.driversById[p.new_driver_id] || {};
    var needed = (Number(p.deadhead_miles || 0) + Number(e ? e.remaining_miles : 0)) / SPEED_MPH;
    return {
      needed: Math.round(needed * 10) / 10,
      left: d.hours_left === undefined ? null : Number(d.hours_left)
    };
  }

  function buildRows() {
    return state.exceptions.map(function (e) {
      var p = latestProposal(e.shipment_id);
      var ship = state.shipments[e.shipment_id] || {};
      var drec = p ? (state.driversById[p.new_driver_id] || {}) : {};
      return {
        e: e,
        p: p,
        ship: ship,
        status: statusOf(p),
        driverId: p ? p.new_driver_id : null,
        driver: p ? (driverName(p.new_driver_id) || p.new_driver_id) : null,
        cost: p ? Number(p.reroute_cost_usd) || 0 : 0,
        deadhead: p ? p.deadhead_miles : null,
        truckId: drec.truck_id || '',
        truckStatus: drec.truck_status || '',
        openDefect: drec.open_defect || '',
        lane: [ship.origin_terminal, ship.dest_terminal].filter(Boolean).join(' to ') || e.lane || ''
      };
    }).sort(function (a, b) {
      var d = ORDER[a.status] - ORDER[b.status];
      return d !== 0 ? d : a.e.shipment_id.localeCompare(b.e.shipment_id);
    });
  }

  /* ------------------------------------------------------------- rendering */
  function renderKpis(rows) {
    var proposals = state.proposals;
    var recovered = proposals.filter(function (p) { return p.decision === 'auto_applied'; });
    var waiting = proposals.filter(function (p) { return statusOf(p) === 'waiting'; });
    var autoCost = recovered.reduce(function (s, p) { return s + (Number(p.reroute_cost_usd) || 0); }, 0);
    var atRisk = rows.reduce(function (s, r) { return s + (Number(r.ship.est_margin_usd) || 0); }, 0);

    var cards = [
      { v: num(rows.length), l: 'Loads at risk', cls: '' },
      { v: num(recovered.length), l: 'Recovered automatically', cls: 'green' },
      { v: num(waiting.length), l: 'Waiting for a dispatcher', cls: 'amber' },
      { v: money(autoCost), l: 'Estimated cost auto-approved', cls: '' },
      { v: money(atRisk), l: 'At-risk revenue on these loads', cls: '' }
    ];
    $('kpis').innerHTML = cards.map(function (c) {
      return '<div class="kpi ' + c.cls + '"><div class="v num">' + esc(c.v) + '</div><div class="l">' + esc(c.l) + '</div></div>';
    }).join('');
  }

  function renderExceptions(rows) {
    $('exCount').textContent = rows.length + (rows.length === 1 ? ' load' : ' loads');
    $('exGrid').innerHTML = rows.map(function (r) {
      var e = r.e;
      var driverLine = r.driver
        ? esc(r.driver) + ' <span class="muted">' + esc(r.driverId) + '</span>'
        : '<span class="muted">No driver proposed yet</span>';
      var facts = num(e.remaining_miles) + ' mi left · ' + num(e.miles) + ' mi total';
      if (e.late_vs_due_hours > 0) facts += ' · ' + num(e.late_vs_due_hours) + ' h behind due';
      var why = r.p ? plainWhy(r.p.conflicts) : 'No proposal yet.';
      var cost = r.p ? money(r.cost) : '—';
      return '' +
        '<article class="ex ' + r.status + '">' +
          '<div class="ex-top">' +
            '<span class="ex-id">' + esc(e.shipment_id) + '</span>' +
            '<span class="ex-cust">' + esc(e.customer) + '</span>' +
          '</div>' +
          '<div class="ex-lane"><span>' + esc(r.lane) + '</span></div>' +
          '<div class="ex-facts">' + esc(e.load_type) + ' · ' + esc(facts) + '</div>' +
          '<div class="ex-driver">' + driverLine + '</div>' +
          (r.truckId ? '<div class="ex-facts">Truck ' + esc(r.truckId) +
            (r.truckStatus ? ' · ' + esc(r.truckStatus) : '') +
            (r.openDefect ? ' · ' + esc(r.openDefect) : '') + '</div>' : '') +
          (hosClock(r.p) ? '<div class="ex-why">' + esc(hosClock(r.p)) + '</div>' : '') +
          '<div class="ex-why">' + esc(why) + '</div>' +
          '<div class="ex-foot">' +
            '<span class="pill ' + r.status + '">' + esc(PILL[r.status] || r.status) + '</span>' +
            '<span class="ex-cost num">' + esc(cost) + '</span>' +
          '</div>' +
        '</article>';
    }).join('');
  }

  function renderCostChart(rows) {
    var W = 640, H = 232, PADL = 48, PADR = 14, PADT = 16, PADB = 34;
    var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'chart', preserveAspectRatio: 'xMidYMid meet' });
    var items = rows.filter(function (r) { return r.p; }).map(function (r) { return { id: r.e.shipment_id, cost: r.cost }; });
    var maxV = Math.max(LIMIT * 1.15, items.reduce(function (m, i) { return Math.max(m, i.cost); }, 0), 1);
    var plotH = H - PADT - PADB, plotW = W - PADL - PADR;
    var y = function (v) { return PADT + plotH - (v / maxV) * plotH; };

    [0, 0.25, 0.5, 0.75, 1].forEach(function (t) {
      var v = maxV * t;
      svg.appendChild(svgEl('line', { x1: PADL, x2: W - PADR, y1: y(v), y2: y(v), stroke: '#1E2A45', 'stroke-width': 1 }));
      svg.appendChild(svgEl('text', { x: PADL - 8, y: y(v) + 4, fill: '#8A94A7', 'font-size': 11, 'text-anchor': 'end' }, '$' + num(Math.round(v))));
    });
    svg.appendChild(svgEl('line', { x1: PADL, x2: W - PADR, y1: y(LIMIT), y2: y(LIMIT), stroke: '#F5A524', 'stroke-width': 1.5, 'stroke-dasharray': '6 5' }));
    svg.appendChild(svgEl('text', { x: W - PADR, y: y(LIMIT) - 7, fill: '#F5A524', 'font-size': 11, 'text-anchor': 'end' }, 'Approval limit $500'));

    if (!items.length) {
      svg.appendChild(svgEl('text', {
        x: PADL + plotW / 2, y: PADT + plotH / 2, fill: '#8A94A7', 'font-size': 12.5, 'text-anchor': 'middle'
      }, 'No proposals yet - this fills in as the copilot works'));
      $('costChart').replaceChildren(svg);
      return;
    }

    var slot = plotW / items.length;
    var bw = Math.min(56, slot * 0.5);
    items.forEach(function (it, i) {
      var cx = PADL + slot * (i + 0.5);
      var over = it.cost > LIMIT;
      var h = Math.max(1.5, (it.cost / maxV) * plotH);
      svg.appendChild(svgEl('rect', {
        x: cx - bw / 2, y: y(it.cost), width: bw, height: h, rx: 3,
        fill: over ? '#D63939' : '#F5A524'
      }));
      svg.appendChild(svgEl('text', { x: cx, y: y(it.cost) - 6, fill: '#E6EAF2', 'font-size': 11, 'text-anchor': 'middle' }, '$' + num(Math.round(it.cost))));
      svg.appendChild(svgEl('text', { x: cx, y: H - 12, fill: '#8A94A7', 'font-size': 10.5, 'text-anchor': 'middle' }, shortId(it.id)));
    });
    $('costChart').replaceChildren(svg);
  }

  function renderHoursChart(rows) {
    var W = 640, H = 232, PADL = 42, PADR = 14, PADT = 16, PADB = 34;
    var svg = svgEl('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'chart', preserveAspectRatio: 'xMidYMid meet' });
    var items = rows.filter(function (r) { return r.p; }).map(function (r) {
      var h = hoursFor(r.p, r.e);
      return { id: r.e.shipment_id, needed: h.needed, left: h.left };
    });
    var maxV = Math.max(items.reduce(function (m, i) {
      return Math.max(m, i.needed || 0, i.left === null ? 0 : i.left);
    }, 0), 4);
    var plotH = H - PADT - PADB, plotW = W - PADL - PADR;
    var y = function (v) { return PADT + plotH - (v / maxV) * plotH; };

    [0, 0.25, 0.5, 0.75, 1].forEach(function (t) {
      var v = maxV * t;
      svg.appendChild(svgEl('line', { x1: PADL, x2: W - PADR, y1: y(v), y2: y(v), stroke: '#1E2A45', 'stroke-width': 1 }));
      svg.appendChild(svgEl('text', { x: PADL - 8, y: y(v) + 4, fill: '#8A94A7', 'font-size': 11, 'text-anchor': 'end' }, (Math.round(v * 10) / 10) + 'h'));
    });

    if (!items.length) {
      svg.appendChild(svgEl('text', {
        x: PADL + plotW / 2, y: PADT + plotH / 2, fill: '#8A94A7', 'font-size': 12.5, 'text-anchor': 'middle'
      }, 'No proposals yet - this fills in as the copilot works'));
      $('hoursChart').replaceChildren(svg);
      return;
    }

    var slot = plotW / items.length;
    var bw = Math.min(22, slot * 0.30);
    items.forEach(function (it, i) {
      var cx = PADL + slot * (i + 0.5);
      var hN = Math.max(1.5, (it.needed / maxV) * plotH);
      svg.appendChild(svgEl('rect', { x: cx - bw - 2, y: y(it.needed), width: bw, height: hN, rx: 2, fill: '#8A94A7' }));
      if (it.left !== null) {
        var short = it.left < it.needed;
        var hL = Math.max(1.5, (it.left / maxV) * plotH);
        svg.appendChild(svgEl('rect', {
          x: cx + 2, y: y(it.left), width: bw, height: hL, rx: 2,
          fill: short ? '#D63939' : '#2FB344'
        }));
        if (short) {
          svg.appendChild(svgEl('text', {
            x: cx + 2 + bw / 2, y: y(it.needed) - 6, fill: '#D63939', 'font-size': 10.5, 'text-anchor': 'middle'
          }, 'short'));
        }
      }
      svg.appendChild(svgEl('text', { x: cx, y: H - 12, fill: '#8A94A7', 'font-size': 10.5, 'text-anchor': 'middle' }, shortId(it.id)));
    });
    $('hoursChart').replaceChildren(svg);
  }

  function renderQueue(rows) {
    var waiting = rows.filter(function (r) { return r.status === 'waiting'; });
    $('qCount').textContent = waiting.length + ' waiting';
    if (!waiting.length) {
      $('queue').innerHTML = '<div class="empty">Nothing is waiting. Every proposal so far either cleared the rules or was already approved.</div>';
      return;
    }
    $('queue').innerHTML = waiting.map(function (r) {
      return '' +
        '<div class="row">' +
          '<span class="s">' + esc(r.e.shipment_id) + '</span>' +
          '<span class="d">' + esc(r.driver || 'No driver') + (r.driverId ? ' · ' + esc(r.driverId) : '') + ' · ' + esc(money(r.cost)) + '</span>' +
          '<span class="st waiting">Waiting</span>' +
        '</div>';
    }).join('');
  }

  function renderActivity() {
    var feed = state.updates.slice(0, 6);
    $('actCount').textContent = state.updates.length + ' events';
    if (!feed.length) {
      $('activity').innerHTML = '<div class="empty">No reroutes have been applied yet.</div>';
      return;
    }
    $('activity').innerHTML = feed.map(function (u) {
      var p = null;
      for (var i = 0; i < state.proposals.length; i++) {
        if (state.proposals[i].proposal_id === u.proposal_id) { p = state.proposals[i]; break; }
      }
      var st = statusOf(p);
      var word = st === 'approved' ? 'Approved' : (st === 'recovered' ? 'Recovered' : 'Applied');
      return '' +
        '<div class="row">' +
          '<span class="t num">' + esc(hhmm(u.ts_utc)) + 'Z</span>' +
          '<span class="s">' + esc(u.shipment_id) + '</span>' +
          '<span class="d">' + esc(u.driver_name || u.driver_id || '') + '</span>' +
          '<span class="st ' + st + '">' + esc(word) + '</span>' +
        '</div>';
    }).join('');
  }

  function renderChecklist(rows) {
    var withProposal = rows.filter(function (r) { return r.p; }).length;
    var allIn = rows.length > 0 && withProposal === rows.length;
    var approved = state.audit.some(function (a) { return a.event === 'proposal.approved'; });
    var r149 = rows.filter(function (r) { return r.e.shipment_id === 'APX-2610149'; })[0];

    var r149waiting = !!(r149 && r149.p && r149.status === 'waiting');
    var items = [
      {
        done: allIn,
        title: 'Copilot proposed a reroute for every exception',
        note: withProposal + ' of ' + rows.length + ' exceptions have a proposal',
        amber: false
      },
      {
        done: allIn && r149waiting,
        title: "Copilot can't approve: APX-2610149 stays waiting",
        note: r149waiting ? 'Still waiting for a dispatcher. The copilot has no approval tool.'
                          : (r149 && r149.p ? 'APX-2610149 is no longer waiting.'
                                            : 'No proposal for APX-2610149 yet.'),
        amber: true
      },
      {
        done: approved,
        title: 'Only a human can approve',
        note: approved ? 'A dispatcher approved a proposal in a separate session.'
                       : 'No approval recorded yet.',
        amber: false
      }
    ];

    $('ckCount').textContent = items.filter(function (i) { return i.done; }).length + ' of ' + items.length;
    var tick = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5l9 -13" /></svg>';
    $('checklist').innerHTML = items.map(function (it, i) {
      return '' +
        '<div class="check' + (it.done ? ' done' : '') + '">' +
          '<span class="n">' + (i + 1) + '</span>' +
          '<span class="box">' + (it.done ? tick : '') + '</span>' +
          '<span class="body">' +
            '<span class="title">' + esc(it.title) + '</span>' +
            '<div class="note' + (it.amber ? ' amber' : '') + '">' + esc(it.note) + '</div>' +
          '</span>' +
        '</div>';
    }).join('');
  }

  function renderSquads() {
    var s = state.squads;
    if (!s) return;
    if (s.title) $('squadsTitle').textContent = s.title;
    var head = '<tr><th>Identity</th><th>Team</th><th>Token limit</th><th>Dispatch tools</th><th>Can approve</th></tr>';
    var body = (s.identities || []).map(function (i) {
      return '' +
        '<tr class="' + (i.in_use ? 'in-use' : '') + '">' +
          '<td class="id">' + esc(i.identity) + '</td>' +
          '<td class="sub">' + esc(i.team) + '</td>' +
          '<td class="num">' + esc(i.token_limit) + '</td>' +
          '<td>' + esc(i.dispatch_tools) + '</td>' +
          '<td>' + esc(i.can_approve) + '</td>' +
        '</tr>';
    }).join('');
    $('squads').innerHTML = '<table class="squads">' + head + body + '</table>';
    $('squadsNote').textContent = s.footer_note || '';
  }

  function render() {
    var rows = buildRows();
    renderKpis(rows);
    renderCostChart(rows);
    renderHoursChart(rows);
    renderExceptions(rows);
    renderQueue(rows);
    renderActivity();
    renderChecklist(rows);
    renderSquads();

    $('liveText').textContent = state.ok ? 'polling every 3s' : 'connection lost - retrying';
    $('liveDot').className = 'live-dot' + (state.ok ? '' : ' stale');
    $('runTime').textContent = 'Run ' + new Date().toISOString().slice(0, 16) + 'Z';
  }

  window.addEventListener('DOMContentLoaded', function () {
    jget('/config/squads.json').then(function (s) { state.squads = s; })
      .catch(function (e) { console.error('board: squads.json failed', e); })
      .then(function () { render(); });
    tick();
    setInterval(tick, POLL_MS);
  });
})();
