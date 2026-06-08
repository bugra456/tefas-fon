"""
TEFAS Fon Tavsiye Sistemi - Tek Dosya Versiyonu
PythonAnywhere veya bilgisayarda çalıştırılabilir
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── TEFAS API ──────────────────────────────────────────────────────────────────

INFO_URL = "https://www.tefas.gov.tr/api/funds/fonGnlBlgSiraliGetir"

API_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/tr/fon-verileri",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}


def api_body(bas_tarih, bit_tarih, fon_tipi="YAT", fon_kodu=None):
    return {
        "fonTipi": fon_tipi, "fonKodu": fon_kodu, "aramaMetni": None,
        "fonTurKod": None, "fonGrubu": None, "sfonTurKod": None,
        "fonTurAciklama": None, "kurucuKod": None,
        "basTarih": bas_tarih, "bitTarih": bit_tarih,
        "basSira": 1, "bitSira": 100000, "dil": "TR",
        "sFonTurKod": "", "fonKod": "", "fonGrup": "", "fonUnvanTip": "",
    }


def tefas_fetch_day(target_date: str, kind: str = "YAT"):
    resp = requests.post(INFO_URL, json=api_body(target_date, target_date, kind), headers=API_HEADERS, timeout=30)
    data = resp.json()
    if data.get("errorCode") or data.get("errorMessage"):
        return []
    return data.get("resultList") or []


def tefas_fetch_fund_history(fund_code: str, days_back: int = 95, kind: str = "YAT"):
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days_back)
    all_rows = []
    cur = start_dt
    while cur <= end_dt:
        chunk_end = min(cur + timedelta(days=27), end_dt)
        body = api_body(cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d"), kind, fund_code.upper())
        try:
            resp = requests.post(INFO_URL, json=body, headers=API_HEADERS, timeout=30)
            rows = resp.json().get("resultList") or []
            all_rows.extend([{"date": r.get("tarih"), "price": r.get("fiyat")} for r in rows])
        except Exception:
            pass
        cur = chunk_end + timedelta(days=1)
    return all_rows


def find_nearest_day(base_date_str, offset_range=range(0, 7)):
    """Verilen tarihte veri yoksa yakın günleri dener."""
    for offset in offset_range:
        d = datetime.strptime(base_date_str, "%Y%m%d") + timedelta(days=offset)
        result = tefas_fetch_day(d.strftime("%Y%m%d"))
        if result:
            return d.strftime("%Y%m%d"), result
    return None, []


# ── SKORLAMA ───────────────────────────────────────────────────────────────────

def calculate_scores(today_list, m1_list, m3_list, deposit_annual):
    deposit_m = deposit_annual / 12
    results = []

    m1_map = {r["fonKodu"]: r for r in m1_list}
    m3_map = {r["fonKodu"]: r for r in m3_list}

    for row in today_list:
        code = row["fonKodu"]
        name = row["fonUnvan"]
        price_now = row.get("fiyat", 0)
        size = row.get("portfoyBuyukluk", 0) or 0
        inv_now = row.get("kisiSayisi", 0) or 0

        m1 = m1_map.get(code)
        m3 = m3_map.get(code)
        price_m1 = m1["fiyat"] if m1 else None
        price_m3 = m3["fiyat"] if m3 else None
        inv_m1 = m1.get("kisiSayisi", 0) if m1 else None

        if not price_m1 or price_m1 <= 0 or not price_m3 or price_m3 <= 0:
            continue

        ret_1m = (price_now / price_m1) - 1
        ret_3m = (price_now / price_m3) - 1
        ann_ret = (1 + ret_3m) ** 4 - 1

        # 1) MOMENTUM (30)
        mom = 0
        if ret_1m > deposit_m * 2: mom += 15
        elif ret_1m > deposit_m: mom += 12
        elif ret_1m > 0: mom += 6
        if ret_3m > deposit_m * 6: mom += 15
        elif ret_3m > deposit_m * 3: mom += 12
        elif ret_3m > 0: mom += 6

        # 2) RİSK-GETİRİ (25)
        risk = 0
        m_3m_avg = ret_3m / 3
        consistency = 1 - abs(ret_1m - m_3m_avg) / max(abs(m_3m_avg), 0.001)
        excess = ret_1m - deposit_m
        rp = excess / max(consistency, 0.01)
        if rp > deposit_m: risk += 25
        elif rp > 0: risk += 18
        elif excess > 0: risk += 10

        # 3) İSTİKRAR (20)
        stab = 0
        pos = sum(1 for r in [ret_1m, ret_3m / 3] if r > 0)
        if pos == 2: stab += 10
        abv = sum(1 for r in [ret_1m, ret_3m / 3] if r > deposit_m)
        if abv == 2: stab += 10
        elif abv == 1: stab += 5
        if size > 1e9: stab += 5
        elif size > 1e8: stab += 3
        stab = min(stab, 20)

        # 4) TREND (15)
        trend = 0
        acc = ret_1m - m_3m_avg
        if acc > deposit_m: trend += 10
        elif acc > 0: trend += 7
        elif acc > -deposit_m: trend += 3
        if ret_1m > 0.03: trend += 5
        elif ret_1m > 0.01: trend += 3
        trend = min(trend, 15)

        # 5) YATIRIMCI (10)
        inv_s = 0.0
        if inv_m1 and inv_m1 > 0:
            chg = (inv_now - inv_m1) / inv_m1
            if chg > 0.05: inv_s += 7
            elif chg > 0: inv_s += 4
            elif chg > -0.02: inv_s += 2
        if inv_now > 1000: inv_s += 3
        elif inv_now > 100: inv_s += 1
        inv_s = min(inv_s, 10)

        total = mom + risk + stab + trend + inv_s

        if total >= 70: rl, pred, picon = "Düşük Risk", "📈 Yükseliş", "up"
        elif total >= 50: rl, pred, picon = "Orta Risk", "➡️ Yatay/Yükseliş", "neutral"
        elif total >= 30: rl, pred, picon = "Yüksek Risk", "⚠️ Belirsiz", "neutral"
        else: rl, pred, picon = "Çok Yüksek Risk", "📉 Düşüş Riski", "down"

        results.append({
            "code": code, "name": name, "price": price_now,
            "size": size, "investors": inv_now,
            "r1m": round(ret_1m * 100, 2), "r3m": round(ret_3m * 100, 2),
            "ann": round(ann_ret * 100, 1),
            "beats": ret_1m > deposit_m,
            "score": round(total, 1),
            "s_mom": round(mom, 1), "s_risk": round(risk, 1),
            "s_stab": round(stab, 1), "s_trend": round(trend, 1),
            "s_inv": round(inv_s, 1),
            "risk_label": rl, "pred": pred, "picon": picon,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def analyze_detail(fund_code, deposit_rate):
    history = tefas_fetch_fund_history(fund_code, days_back=95)
    if len(history) < 5:
        return None

    prices = [h["price"] for h in history]
    dates = [h["date"] for h in history]
    daily_ret = np.diff(prices) / prices[:-1]

    total_ret = (prices[-1] / prices[0]) - 1

    # Takvim günü bazlı hesaplama (ana sayfa ile tutarlı)
    last_date = datetime.strptime(dates[-1], "%Y-%m-%d")
    target_30 = (last_date - timedelta(days=30)).strftime("%Y-%m-%d")
    target_60 = (last_date - timedelta(days=60)).strftime("%Y-%m-%d")

    # En yakın tarihi bul
    def nearest_price(target_dt):
        best_idx, best_diff = None, 999
        for i, d in enumerate(dates):
            diff = abs((datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(target_dt, "%Y-%m-%d")).days)
            if diff < best_diff:
                best_idx, best_diff = i, diff
        return prices[best_idx] if best_idx is not None else prices[0]

    p30 = nearest_price(target_30)
    p60 = nearest_price(target_60)
    ret30 = (prices[-1] / p30) - 1
    ret60 = (prices[-1] / p60) - 1
    vol = float(np.std(daily_ret) * np.sqrt(252))
    cummax = np.maximum.accumulate(prices)
    maxdd = float(np.max((cummax - prices) / cummax))
    avg_dr = float(np.mean(daily_ret))
    ann_ret = (1 + avg_dr) ** 252 - 1
    sharpe = float((ann_ret - deposit_rate) / vol) if vol > 0 else 0

    if len(prices) >= 10:
        x = np.arange(10)
        slope = float(np.polyfit(x, prices[-10:], 1)[0])
        tdir = "Yükseliş 🔼" if slope > 0 else "Düşüş 🔽"
        tstr = abs(slope / np.mean(prices[-10:])) * 100
    else:
        tdir, tstr = "N/A", 0

    ma10 = float(np.mean(prices[-10:])) if len(prices) >= 10 else prices[-1]
    ma20 = float(np.mean(prices[-20:])) if len(prices) >= 20 else prices[-1]
    ma_sig = "🟢 AL" if ma10 > ma20 else "🔴 SAT"

    return {
        "code": fund_code, "prices": prices[-30:], "dates": dates[-30:],
        "total_ret": round(total_ret * 100, 2), "ret30": round(ret30 * 100, 2),
        "ret60": round(ret60 * 100, 2), "ann_ret": round(ann_ret * 100, 1),
        "vol": round(vol * 100, 2), "maxdd": round(maxdd * 100, 2),
        "sharpe": round(sharpe, 2), "tdir": tdir, "tstr": round(tstr, 4),
        "ma_sig": ma_sig, "ma10": round(ma10, 6), "ma20": round(ma20, 6),
        "cur_price": round(prices[-1], 6), "npoints": len(prices),
    }


# ── HTML TEMPLATE ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TEFAS Fon Tavsiye Sistemi</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e27;--bg2:#111638;--card:#161b40;--cardh:#1c224d;--acc:#4f8cff;
--accl:#7aa8ff;--grn:#00d68f;--red:#ff4757;--org:#ffa502;--txt:#e8eaf6;
--txt2:#8b8fb0;--brd:#252a54;--gold:#ffd700}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh}
body::before{content:'';position:fixed;inset:0;
background:radial-gradient(ellipse at 20% 50%,rgba(79,140,255,.08) 0%,transparent 50%),
radial-gradient(ellipse at 80% 20%,rgba(0,214,143,.05) 0%,transparent 50%);
z-index:0;pointer-events:none}
.ctn{max-width:1400px;margin:0 auto;padding:20px;position:relative;z-index:1}
.hdr{text-align:center;padding:30px 0 20px}
.hdr h1{font-size:2.2rem;font-weight:700;
background:linear-gradient(135deg,var(--acc),var(--grn));
-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:8px}
.hdr p{color:var(--txt2);font-size:1rem}
.inp-sec{background:var(--card);border:1px solid var(--brd);border-radius:16px;padding:30px;margin:20px 0;text-align:center}
.inp-sec h2{color:var(--accl);margin-bottom:20px;font-size:1.2rem}
.inp-g{display:flex;align-items:center;justify-content:center;gap:15px;flex-wrap:wrap}
.inp-g input{background:var(--bg);border:2px solid var(--brd);border-radius:10px;
padding:12px 18px;color:var(--txt);font-size:1.3rem;width:180px;text-align:center}
.inp-g input:focus{outline:none;border-color:var(--acc)}
.btn{background:linear-gradient(135deg,var(--acc),#6366f1);color:#fff;border:none;
padding:14px 40px;border-radius:12px;font-size:1.1rem;font-weight:600;cursor:pointer;
box-shadow:0 4px 15px rgba(79,140,255,.3);transition:all .3s}
.btn:hover{transform:translateY(-2px);box-shadow:0 6px 25px rgba(79,140,255,.4)}
.btn:disabled{opacity:.6;cursor:not-allowed;transform:none}
.sum-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:20px 0}
.sum-card{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;text-align:center}
.sum-card .lb{font-size:.85rem;color:var(--txt2);margin-bottom:6px}
.sum-card .vl{font-size:1.8rem;font-weight:700}
.stitle{display:flex;align-items:center;gap:10px;margin:30px 0 15px;font-size:1.4rem;font-weight:700}
.rec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:16px;margin-bottom:30px}
.fc{background:var(--card);border:1px solid var(--brd);border-radius:14px;padding:20px;
cursor:pointer;transition:all .3s;position:relative;overflow:hidden;text-decoration:none;color:inherit;display:block}
.fc:hover{border-color:var(--acc);background:var(--cardh);transform:translateY(-2px);
box-shadow:0 8px 30px rgba(79,140,255,.15)}
.rank{position:absolute;top:10px;right:10px;background:linear-gradient(135deg,var(--acc),#6366f1);
color:#fff;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;
justify-content:center;font-weight:700;font-size:.9rem}
.fc .fh{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.fc .fcode{background:var(--bg);padding:4px 12px;border-radius:6px;font-weight:700;font-size:1rem;
color:var(--accl);letter-spacing:1px}
.fc .fname{font-size:.85rem;color:var(--txt2);line-height:1.3;flex:1}
.pred{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;
font-size:.8rem;font-weight:600;margin-bottom:10px}
.pred.up{background:rgba(0,214,143,.15);color:var(--grn)}
.pred.neutral{background:rgba(255,165,2,.15);color:var(--org)}
.pred.down{background:rgba(255,71,87,.15);color:var(--red)}
.mets{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.met{background:var(--bg);padding:8px 12px;border-radius:8px}
.met .ml{font-size:.7rem;color:var(--txt2)}
.met .mv{font-size:1rem;font-weight:600}
.mv.pos{color:var(--grn)}.mv.neg{color:var(--red)}
.sbar{margin-top:12px;height:6px;background:var(--bg);border-radius:3px;overflow:hidden}
.sbar-f{height:100%;border-radius:3px;transition:width .8s}
.sbar-l{display:flex;justify-content:space-between;margin-top:4px;font-size:.75rem;color:var(--txt2)}
.sbd{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;margin-top:8px;text-align:center}
.sbd .sl{font-size:.6rem;color:var(--txt2)}.sbd .sv{font-size:.7rem;font-weight:600}
.tc{background:var(--card);border:1px solid var(--brd);border-radius:14px;overflow:hidden;margin:20px 0}
.tabs{display:flex;border-bottom:1px solid var(--brd)}
.tab{padding:12px 24px;cursor:pointer;color:var(--txt2);font-weight:500;border-bottom:2px solid transparent;transition:.3s}
.tab.act{color:var(--accl);border-bottom-color:var(--acc);background:rgba(79,140,255,.05)}
.tab:hover{color:var(--txt)}
table{width:100%;border-collapse:collapse}
table th{background:var(--bg2);padding:12px 16px;text-align:left;font-size:.8rem;
color:var(--txt2);text-transform:uppercase;letter-spacing:.5px}
table td{padding:12px 16px;border-bottom:1px solid var(--brd);font-size:.9rem}
table tbody tr:hover{background:var(--cardh);cursor:pointer}
.disc{background:rgba(255,165,2,.08);border:1px solid rgba(255,165,2,.2);border-radius:10px;
padding:15px;margin:20px 0;font-size:.8rem;color:var(--org);text-align:center}
.loading{display:none;position:fixed;inset:0;background:rgba(10,14,39,.9);z-index:200;
justify-content:center;align-items:center;flex-direction:column;gap:20px}
.loading.act{display:flex}
.spin{width:50px;height:50px;border:4px solid var(--brd);border-top-color:var(--acc);
border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.det-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin:20px 0}
.det-item{background:var(--bg);padding:12px;border-radius:8px}
.det-item .dl{font-size:.75rem;color:var(--txt2);margin-bottom:4px}
.det-item .dv{font-size:1.1rem;font-weight:600}
.chart-box{background:var(--bg);border-radius:12px;padding:20px;margin-top:20px}
.chart-box h3{color:var(--txt2);font-size:.9rem;margin-bottom:15px}
.back-link{display:inline-block;margin-bottom:20px;color:var(--acc);text-decoration:none;
font-size:1rem;border:1px solid var(--brd);padding:8px 16px;border-radius:8px}
.back-link:hover{background:var(--card)}
.hidden{display:none!important}
@media(max-width:768px){.ctn{padding:10px}.hdr h1{font-size:1.5rem}
.rec-grid{grid-template-columns:1fr}.inp-g{flex-direction:column}
.inp-g input{width:100%}.btn{width:100%}table{font-size:.8rem}
table th,table td{padding:8px}}
</style>
</head>
<body>
<div class="ctn">
<div class="hdr">
<h1>📊 TEFAS Fon Tavsiye Sistemi</h1>
<p>Mevduat faizinin üzerinde getiri potansiyeli olan yatırım fonlarını keşfedin</p>
</div>

{% block content %}{% endblock %}
</div>

<div class="loading" id="loading">
<div class="spin"></div>
<div style="color:var(--txt2)" id="loadTxt">Yükleniyor...</div>
</div>

<script>
function $(s){return document.querySelector(s)}
function showLoad(t){$('#loadTxt').textContent=t;$('#loading').classList.add('act')}
function hideLoad(){$('#loading').classList.remove('act')}
</script>
</body>
</html>
"""

# ── PAGES ──────────────────────────────────────────────────────────────────────

HOME_PAGE = HTML_TEMPLATE.replace(r"{% block content %}{% endblock %}", r"""
{% block content %}
<div class="inp-sec">
<h2>🏦 Banka Mevduat Faiz Oranınızı Girin</h2>
<form action="/analyze" method="POST" class="inp-g" onsubmit="showLoad('TEFAS verileri çekiliyor... Bu işlem 10-15 saniye sürebilir')">
<label>Yıllık Brüt Mevduat Faizi:</label>
<input type="number" name="rate" value="40" min="1" max="200" step="0.5" required>
<label>%</label>
<button type="submit" class="btn">🔍 Fonları Analiz Et</button>
</form>
<p style="color:var(--txt2);font-size:.8rem;margin-top:12px">
Stopaj (%15) otomatik olarak düşülerek net mevduat getirisi hesaplanacaktır.
</p>
</div>
{% endblock %}
""")

RESULTS_PAGE = HTML_TEMPLATE.replace(r"{% block content %}{% endblock %}", r"""
{% block content %}
<a href="/" class="back-link">← Yeni Analiz</a>

<div class="sum-row">
<div class="sum-card"><div class="lb">Analiz Tarihi</div><div class="vl" style="font-size:1.2rem;color:var(--acc)">{{ date }}</div></div>
<div class="sum-card"><div class="lb">Mevduat Aylık Net (%)</div><div class="vl" style="color:var(--org)">{{ dep_net }}%</div></div>
<div class="sum-card"><div class="lb">Analiz Edilen Fon</div><div class="vl" style="color:var(--acc)">{{ total }}</div></div>
<div class="sum-card"><div class="lb">Mevduat Üstü Fon</div><div class="vl" style="color:var(--grn)">{{ above }}</div></div>
</div>

<div class="stitle"><span style="font-size:1.6rem">⭐</span> Önerilen Fonlar (Önümüzdeki Ay)</div>
<div class="rec-grid">
{% for f in recommended %}
<a href="/fund/{{ f.code }}?rate={{ rate }}" class="fc" style="text-decoration:none;color:inherit">
<div class="rank">{{ loop.index }}</div>
<div class="fh">
<span class="fcode">{{ f.code }}</span>
<span class="fname">{{ f.name }}</span>
</div>
<div class="pred {{ f.picon }}">{{ f.pred }}</div>
<div class="mets">
<div class="met"><div class="ml">Fiyat</div><div class="mv">{{ "%.4f"|format(f.price) }}</div></div>
<div class="met"><div class="ml">1 Ay</div><div class="mv {{ 'pos' if f.r1m > 0 else 'neg' }}">{{ "%+.2f"|format(f.r1m) }}%</div></div>
<div class="met"><div class="ml">3 Ay</div><div class="mv {{ 'pos' if f.r3m > 0 else 'neg' }}">{{ "%+.2f"|format(f.r3m) }}%</div></div>
<div class="met"><div class="ml">Yıllıklaştırılmış</div><div class="mv {{ 'pos' if f.ann > 0 else 'neg' }}">{{ "%+.1f"|format(f.ann) }}%</div></div>
<div class="met"><div class="ml">Portföy (₺)</div><div class="mv" style="font-size:.85rem">{{ fmt_size(f.size) }}</div></div>
<div class="met"><div class="ml">Yatırımcı</div><div class="mv">{{ fmt_num(f.investors) }}</div></div>
</div>
{% set sc = "#00d68f" if f.score >= 70 else ("#4f8cff" if f.score >= 50 else ("#ffa502" if f.score >= 30 else "#ff4757")) %}
<div class="sbar"><div class="sbar-f" style="width:{{ f.score }}%;background:{{ sc }}"></div></div>
<div class="sbar-l"><span>Toplam Skor</span><span style="color:{{ sc }};font-weight:700">{{ f.score }}/100</span></div>
<div class="sbd">
<div><div class="sl">Momentum</div><div class="sv">{{ f.s_mom }}/30</div></div>
<div><div class="sl">Risk-Getiri</div><div class="sv">{{ f.s_risk }}/25</div></div>
<div><div class="sl">İstikrar</div><div class="sv">{{ f.s_stab }}/20</div></div>
<div><div class="sl">Trend</div><div class="sv">{{ f.s_trend }}/15</div></div>
<div><div class="sl">Yatırımcı</div><div class="sv">{{ f.s_inv }}/10</div></div>
</div>
</a>
{% endfor %}
</div>

{% if not recommended %}
<p style="color:var(--txt2);padding:20px;text-align:center">Kriterlere uygun fon bulunamadı. Düşük faiz oranı deneyin.</p>
{% endif %}

<div class="disc">
⚠️ <strong>Önemli Uyarı:</strong> Bu uygulama algoritmik analiz yapmaktadır ve yatırım tavsiyesi değildir.
Geçmiş performans gelecekteki getirilerin garantisi değildir. Yatırım kararlarınızı kendiniz verin.
</div>

<div class="stitle"><span style="font-size:1.6rem">📋</span> Tüm Fonlar</div>
<div class="tc">
<div class="tabs">
<div class="tab act" onclick="showTab('above',this)">Mevduat Üstü ({{ above }})</div>
<div class="tab" onclick="showTab('all',this)">Tüm Fonlar ({{ total }})</div>
</div>
<div style="overflow-x:auto">
<table>
<thead><tr><th>#</th><th>Kod</th><th>Fon Adı</th><th>Fiyat</th><th>1A %</th><th>3A %</th><th>Yıllık %</th><th>Skor</th><th>Tahmin</th><th>Risk</th></tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>
</div>

<script>
const allFunds = {{ all_json|safe }};
const aboveFunds = {{ above_json|safe }};
let curTab = 'above';

function renderTable(funds) {
    const tb = document.getElementById('tbody');
    tb.innerHTML = '';
    funds.forEach((f,i) => {
        const sc = f.score>=70?'color:#00d68f':f.score>=50?'color:#4f8cff':f.score>=30?'color:#ffa502':'color:#ff4757';
        tb.innerHTML += `<tr onclick="location.href='/fund/${f.code}?rate={{ rate }}'">
            <td>${i+1}</td>
            <td><strong style="color:var(--accl)">${f.code}</strong></td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${f.name}</td>
            <td>${f.price.toFixed(4)}</td>
            <td style="color:${f.r1m>0?'var(--grn)':'var(--red)'};font-weight:600">${f.r1m>0?'+':''}${f.r1m}%</td>
            <td style="color:${f.r3m>0?'var(--grn)':'var(--red)'};font-weight:600">${f.r3m>0?'+':''}${f.r3m}%</td>
            <td style="color:${f.ann>0?'var(--grn)':'var(--red)'}">${f.ann>0?'+':''}${f.ann}%</td>
            <td style="${sc};font-weight:700">${f.score}</td>
            <td>${f.pred}</td>
            <td>${f.risk_label}</td>
        </tr>`;
    });
}

function showTab(tab, el) {
    curTab = tab;
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('act'));
    el.classList.add('act');
    renderTable(tab==='above'?aboveFunds:allFunds);
}
renderTable(aboveFunds);
</script>
{% endblock %}
""")

DETAIL_PAGE = HTML_TEMPLATE.replace(r"{% block content %}{% endblock %}", r"""
{% block content %}
<a href="/results?rate={{ rate }}" class="back-link">← Sonuçlara Dön</a>
<a href="/" class="back-link" style="margin-left:8px">🏠 Ana Sayfa</a>

<h2 style="color:var(--accl);margin:20px 0">{{ d.code }} Detaylı Analiz</h2>

<div class="det-grid">
<div class="det-item"><div class="dl">Güncel Fiyat</div><div class="dv">{{ d.cur_price }}</div></div>
<div class="det-item"><div class="dl">Son 30 Gün</div><div class="dv" style="color:{{ '#00d68f' if d.ret30>0 else '#ff4757' }}">{{ "%+.2f"|format(d.ret30) }}%</div></div>
<div class="det-item"><div class="dl">Son 60 Gün</div><div class="dv" style="color:{{ '#00d68f' if d.ret60>0 else '#ff4757' }}">{{ "%+.2f"|format(d.ret60) }}%</div></div>
<div class="det-item"><div class="dl">Toplam Dönem</div><div class="dv" style="color:{{ '#00d68f' if d.total_ret>0 else '#ff4757' }}">{{ "%+.2f"|format(d.total_ret) }}%</div></div>
<div class="det-item"><div class="dl">Yıllıklaştırılmış</div><div class="dv" style="color:{{ '#00d68f' if d.ann_ret>0 else '#ff4757' }}">{{ "%+.1f"|format(d.ann_ret) }}%</div></div>
<div class="det-item"><div class="dl">Volatilite (Yıllık)</div><div class="dv">{{ d.vol }}%</div></div>
<div class="det-item"><div class="dl">Max Drawdown</div><div class="dv" style="color:#ff4757">-{{ d.maxdd }}%</div></div>
<div class="det-item"><div class="dl">Sharpe Oranı</div><div class="dv" style="color:{{ '#00d68f' if d.sharpe>1 else ('#4f8cff' if d.sharpe>0 else '#ff4757') }}">{{ d.sharpe }}</div></div>
<div class="det-item"><div class="dl">Trend</div><div class="dv">{{ d.tdir }}</div></div>
<div class="det-item"><div class="dl">MA Sinyal</div><div class="dv" style="font-size:1.3rem">{{ d.ma_sig }}</div></div>
<div class="det-item"><div class="dl">MA 10</div><div class="dv">{{ d.ma10 }}</div></div>
<div class="det-item"><div class="dl">MA 20</div><div class="dv">{{ d.ma20 }}</div></div>
</div>

<div class="chart-box">
<h3>📈 Son 30 Gün Fiyat Grafiği</h3>
<canvas id="chart" width="800" height="250" style="width:100%;max-height:250px"></canvas>
</div>

<p style="color:var(--txt2);font-size:.75rem;margin-top:15px;text-align:center">{{ d.npoints }} günlük veri ile analiz edilmiştir</p>

<script>
const prices = {{ prices_json|safe }};
const dates = {{ dates_json|safe }};
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');

function draw(){
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width; canvas.height = 250;
    const w=canvas.width-80, h=canvas.height-50, pad={t:20,l:60,r:20,b:30};
    const mn=Math.min(...prices),mx=Math.max(...prices),rng=mx-mn||1;

    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.strokeStyle='rgba(255,255,255,.05)';ctx.lineWidth=1;
    for(let i=0;i<=4;i++){
        const y=pad.t+(h/4)*i;
        ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(canvas.width-pad.r,y);ctx.stroke();
        ctx.fillStyle='#8b8fb0';ctx.font='10px sans-serif';ctx.textAlign='right';
        ctx.fillText((mx-(rng/4)*i).toFixed(4),pad.l-5,y+3);
    }

    ctx.beginPath();ctx.strokeStyle='#4f8cff';ctx.lineWidth=2;
    prices.forEach((p,i)=>{
        const x=pad.l+(i/(prices.length-1))*w;
        const y=pad.t+h-((p-mn)/rng)*h;
        i===0?ctx.moveTo(x,y):ctx.lineTo(x,y);
    });
    ctx.stroke();

    const grad=ctx.createLinearGradient(0,pad.t,0,canvas.height-pad.b);
    grad.addColorStop(0,'rgba(79,140,255,.15)');grad.addColorStop(1,'rgba(79,140,255,0)');
    ctx.lineTo(pad.l+w,pad.t+h);ctx.lineTo(pad.l,pad.t+h);ctx.closePath();
    ctx.fillStyle=grad;ctx.fill();

    ctx.fillStyle='#8b8fb0';ctx.font='9px sans-serif';ctx.textAlign='center';
    const step=Math.max(1,Math.floor(dates.length/6));
    dates.forEach((d,i)=>{if(i%step===0){
        const x=pad.l+(i/(dates.length-1))*w;
        ctx.fillText(d.slice(5),x,canvas.height-5);
    }});

    const lx=pad.l+w,ly=pad.t+h-((prices[prices.length-1]-mn)/rng)*h;
    ctx.beginPath();ctx.arc(lx,ly,4,0,Math.PI*2);ctx.fillStyle='#4f8cff';ctx.fill();
    ctx.strokeStyle='#fff';ctx.lineWidth=2;ctx.stroke();
}
draw();
window.addEventListener('resize',draw);
</script>
{% endblock %}
""")


# ── ROUTES ─────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template_string(HOME_PAGE)


@app.route("/analyze", methods=["POST"])
def analyze():
    rate = float(request.form.get("rate", 40))
    if rate <= 0 or rate > 200:
        return "Geçersiz faiz oranı", 400

    dep_decimal = rate / 100
    dep_net = dep_decimal * 0.85  # Stopaj sonrası
    dep_monthly = dep_net / 12

    try:
        # Bugünün verisi
        logger.info("Bugün çekiliyor...")
        today = datetime.now()
        today_str = today.strftime("%Y%m%d")
        today_list = tefas_fetch_day(today_str)
        actual_date = today_str

        if not today_list:
            for off in range(1, 7):
                d = today - timedelta(days=off)
                today_list = tefas_fetch_day(d.strftime("%Y%m%d"))
                if today_list:
                    actual_date = d.strftime("%Y%m%d")
                    break

        if not today_list:
            return render_template_string(HTML_TEMPLATE.replace(
                r"{% block content %}{% endblock %}",
                r'{% block content %}<div class="inp-sec"><h2 style="color:var(--red)">TEFAS\'tan veri alınamadı. Lütfen daha sonra tekrar deneyin.</h2><a href="/" class="btn" style="display:inline-block;margin-top:20px;text-decoration:none">← Geri Dön</a></div>{% endblock %}'
            ))

        actual_date_fmt = today_list[0].get("tarih", actual_date)
        logger.info(f"Bugün: {actual_date_fmt}, {len(today_list)} fon")

        # 1 ay önce (TEFAS ile aynı: tam 30 gün)
        logger.info("1 ay önce çekiliyor...")
        m1_base = (datetime.strptime(actual_date_fmt, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y%m%d")
        _, m1_list = find_nearest_day(m1_base, offset_range=range(0, 5))
        logger.info(f"1a: {len(m1_list)} fon")

        # 3 ay önce (TEFAS ile aynı: tam 90 gün)
        logger.info("3 ay önce çekiliyor...")
        m3_base = (datetime.strptime(actual_date_fmt, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y%m%d")
        _, m3_list = find_nearest_day(m3_base, offset_range=range(0, 5))
        logger.info(f"3a: {len(m3_list)} fon")

        # Skorları hesapla
        scores = calculate_scores(today_list, m1_list, m3_list, dep_net)

        above_list = [s for s in scores if s["beats"]]
        recommended = [s for s in above_list if s["score"] >= 40][:6]

        import json as _json

        def fmt_size(n):
            if n >= 1e9: return f"{n/1e9:.1f} Mrd"
            if n >= 1e6: return f"{n/1e6:.1f} M"
            if n >= 1e3: return f"{n/1e3:.0f} B"
            return str(int(n))

        def fmt_num(n):
            return f"{int(n):,}".replace(",", ".")

        return render_template_string(RESULTS_PAGE,
            date=actual_date_fmt,
            dep_net=round(dep_monthly * 100, 2),
            total=len(scores),
            above=len(above_list),
            rate=rate,
            recommended=recommended,
            all_json=_json.dumps(scores[:30]),
            above_json=_json.dumps(above_list[:15]),
            fmt_size=fmt_size,
            fmt_num=fmt_num,
        )

    except Exception as e:
        logger.error(f"Hata: {e}", exc_info=True)
        return f"Analiz hatası: {e}", 500


@app.route("/results")
def results_page():
    """Sonuçlar sayfasına geri dön - rate parametresiyle"""
    rate = request.args.get("rate", 40)
    return render_template_string(HTML_TEMPLATE.replace(
        r"{% block content %}{% endblock %}",
        f"""{{% block content %}}
        <div class="inp-sec">
            <h2>Yeniden analiz yapmak ister misiniz?</h2>
            <form action="/analyze" method="POST" class="inp-g" onsubmit="showLoad('TEFAS verileri çekiliyor...')">
                <label>Yıllık Brüt Mevduat Faizi:</label>
                <input type="number" name="rate" value="{rate}" min="1" max="200" step="0.5" required>
                <label>%</label>
                <button type="submit" class="btn">🔍 Tekrar Analiz Et</button>
            </form>
        </div>
        {{% endblock %}}"""
    ))


@app.route("/fund/<code>")
def fund_detail_page(code):
    rate = float(request.args.get("rate", 40))
    detail = analyze_detail(code.upper(), rate / 100)

    if not detail:
        return render_template_string(HTML_TEMPLATE.replace(
            r"{% block content %}{% endblock %}",
            f"""{{% block content %}}
            <div class="inp-sec">
                <h2 style="color:var(--red)">{code.upper()} için yeterli veri bulunamadı</h2>
                <a href="/" class="btn" style="display:inline-block;margin-top:20px;text-decoration:none">← Geri Dön</a>
            </div>
            {{% endblock %}}"""
        ))

    import json as _json

    return render_template_string(DETAIL_PAGE,
        d=detail,
        rate=rate,
        prices_json=_json.dumps(detail["prices"]),
        dates_json=_json.dumps(detail["dates"]),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
