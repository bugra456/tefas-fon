"""
TEFAS Fon Tavsiye Sistemi
Hedef: Mevduat faizini güvenle geçebilecek fonları bulmak
"""
import logging
from datetime import datetime, timedelta
import calendar
import time

import numpy as np
import requests
from flask import Flask, render_template_string, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── TEFAS API ──────────────────────────────────────────────────────────────────

def subtract_months(dt, months):
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    max_day = calendar.monthrange(year, month)[1]
    day = min(dt.day, max_day)
    return dt.replace(year=year, month=month, day=day)


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
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2)
            resp = requests.post(INFO_URL, json=api_body(target_date, target_date, kind), headers=API_HEADERS, timeout=30)
            data = resp.json()
            if data.get("errorCode") or data.get("errorMessage"):
                return []
            return data.get("resultList") or []
        except Exception:
            if attempt == 2:
                return []


def find_nearest_day(base_date_str, offset_range=range(0, 7)):
    for offset in offset_range:
        d = datetime.strptime(base_date_str, "%Y%m%d") + timedelta(days=offset)
        result = tefas_fetch_day(d.strftime("%Y%m%d"))
        if result:
            return d.strftime("%Y%m%d"), result
    return None, []


# ── RİSK SEVİYESİ (TEFAS ile aynı) ────────────────────────────────────────────

def get_risk_level(fund_name):
    """Fon adından TEFAS risk seviyesini hesaplar (1-7).
    1 = En düşük risk (Para Piyasası)
    7 = En yüksek risk (Serbest)
    """
    n = fund_name.upper()
    if "PARA PİYASASI" in n or "PARA PIYASASI" in n:
        return 1
    if "BORÇLANMA" in n or "BORCLANMA" in n or "TAHVİL" in n or "TAHVIL" in n:
        return 2
    if "KAMU" in n and ("BORÇ" in n or "TAHVİL" in n):
        return 2
    if "ALTIN" in n:
        return 4
    if "GÜMÜŞ" in n or "GUMUS" in n:
        return 4
    if "DÖVİZ" in n or "DOVIZ" in n or "DOLAR" in n or "EURO" in n:
        return 4
    if "KARMA" in n:
        return 4
    if "EMTİA" in n or "EMTIA" in n:
        return 5
    if "DEĞİŞKEN" in n or "DEGISKEN" in n:
        return 5
    if "HİSSE" in n or "HISSE" in n:
        return 6
    if "SERBEST" in n:
        return 7
    return 5  # Bilinmeyen = orta-yüksek


RISK_LABELS = {
    1: ("1/7 - Çok Düşük", "#00d68f"),
    2: ("2/7 - Düşük", "#4f8cff"),
    3: ("3/7 - Orta Düşük", "#4f8cff"),
    4: ("4/7 - Orta", "#ffa502"),
    5: ("5/7 - Orta Yüksek", "#ffa502"),
    6: ("6/7 - Yüksek", "#ff4757"),
    7: ("7/7 - Çok Yüksek", "#ff4757"),
}


# ── SKORLAMA: Mevduatı Geçme Analizi ──────────────────────────────────────────

def calculate_scores(today_list, m1_list, m3_list, m6_list, deposit_annual):
    """
    Her fon için: mevduatı geçme olasılığı + güvenilirlik puanı.
    
    Puan ne anlama gelir:
    - Yüksek puan = Mevduatı geçme olasılığı yüksek VE riski düşük
    - Düşük puan = Ya riski yüksek, ya da geçmişte mevduatı geçememiş
    """
    deposit_m = deposit_annual / 12  # Aylık net mevduat getirisi
    results = []

    m1_map = {r["fonKodu"]: r for r in m1_list}
    m3_map = {r["fonKodu"]: r for r in m3_list}
    m6_map = {r["fonKodu"]: r for r in m6_list}

    for row in today_list:
        code = row["fonKodu"]
        name = row["fonUnvan"]
        price_now = row.get("fiyat", 0)
        size = row.get("portfoyBuyukluk", 0) or 0
        inv_now = row.get("kisiSayisi", 0) or 0

        # Filtre: işlem görmeyen fonları ele
        if price_now <= 0:
            continue
        if (row.get("tedPaySayisi") or 0) <= 0:
            continue
        if inv_now < 200:
            continue

        m1 = m1_map.get(code)
        m3 = m3_map.get(code)
        m6 = m6_map.get(code)
        price_m1 = m1["fiyat"] if m1 else None
        price_m3 = m3["fiyat"] if m3 else None
        price_m6 = m6["fiyat"] if m6 else None

        if not price_m1 or price_m1 <= 0 or not price_m3 or price_m3 <= 0:
            continue

        ret_1m = (price_now / price_m1) - 1
        ret_3m = (price_now / price_m3) - 1
        ret_6m = (price_now / price_m6) - 1 if price_m6 and price_m6 > 0 else None

        # TEFAS risk seviyesi
        risk = get_risk_level(name)
        risk_label, risk_color = RISK_LABELS.get(risk, ("?/7", "#fff"))

        # ── MEVDUATI GEÇME KONTROLÜ ──
        beat_1m = ret_1m > deposit_m  # Son 1 ayda mevduatı geçti mi?
        beat_3m_avg = (ret_3m / 3) > deposit_m  # Son 3 ay ortalamada geçti mi?
        beat_6m_avg = (ret_6m / 6 > deposit_m) if ret_6m is not None else False

        beats_count = sum([beat_1m, beat_3m_avg, beat_6m_avg])

        # ── PUAN HESABI ──
        # 1) Mevduatı Geçme Sıklığı (35 puan) - 3 dönemden kaçında geçti?
        if beats_count == 3:
            beat_score = 35
        elif beats_count == 2:
            beat_score = 25
        elif beats_count == 1:
            beat_score = 12
        else:
            beat_score = 0

        # 2) Risk Seviyesi (25 puan) - Düşük risk = öngörülebilir = güvenli
        risk_score = max(0, 25 - (risk - 1) * 4)  # Risk 1→25, 7→1

        # 3) Getiri İstikrarı (20 puan) - 1 aylık ile 3 aylık ort. arası fark küçük mü?
        if ret_3m > 0:
            avg_3m = ret_3m / 3
            stability = 1 - min(abs(ret_1m - avg_3m) / max(abs(avg_3m), 0.001), 1)
            stab_score = round(stability * 20)
        else:
            stab_score = 0

        # 4) Fon Büyüklüğü (10 puan) - Büyük fon = daha güvenli, daha likit
        if size > 5e9:
            size_score = 10
        elif size > 1e9:
            size_score = 8
        elif size > 100e6:
            size_score = 5
        else:
            size_score = 2

        # 5) Yatırımcı Güveni (10 puan) - Çok yatırımcı = güvenilir
        if inv_now > 50000:
            inv_score = 10
        elif inv_now > 10000:
            inv_score = 7
        elif inv_now > 1000:
            inv_score = 4
        else:
            inv_score = 1

        total = beat_score + risk_score + stab_score + size_score + inv_score

        # Mevduatı geçme olasılığı tahmini
        if beats_count == 3 and risk <= 2:
            probability = "Çok Yüksek (%85+)"
        elif beats_count >= 2 and risk <= 3:
            probability = "Yüksek (%70-85)"
        elif beats_count >= 2:
            probability = "Orta (%50-70)"
        elif beats_count == 1 and risk <= 3:
            probability = "Orta (%50-65)"
        elif beats_count == 1:
            probability = "Düşük (%30-50)"
        else:
            probability = "Çok Düşük (%15-30)"

        # Beklenen aylık getiri tahmini (konservatif)
        if ret_6m is not None:
            expected_monthly = (ret_6m / 6) * 0.7  # %30 güvenlik indirimi
        elif ret_3m > 0:
            expected_monthly = (ret_3m / 3) * 0.6
        else:
            expected_monthly = ret_1m * 0.4

        results.append({
            "code": code, "name": name, "price": price_now,
            "size": size, "investors": inv_now,
            "risk": risk, "risk_label": risk_label, "risk_color": risk_color,
            "r1m": round(ret_1m * 100, 2),
            "r3m": round(ret_3m * 100, 2),
            "r6m": round(ret_6m * 100, 2) if ret_6m is not None else None,
            "beat_1m": beat_1m, "beat_3m": beat_3m_avg, "beat_6m": beat_6m_avg,
            "beats": beat_1m,
            "score": round(total, 1),
            "s_beat": beat_score, "s_risk": risk_score,
            "s_stab": stab_score, "s_size": size_score, "s_inv": inv_score,
            "probability": probability,
            "expected_monthly": round(expected_monthly * 100, 2),
            "deposit_monthly": round(deposit_m * 100, 2),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


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
--txt2:#8b8fb0;--brd:#252a54}
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
.sum-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin:20px 0}
.sum-card{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;text-align:center}
.sum-card .lb{font-size:.85rem;color:var(--txt2);margin-bottom:6px}
.sum-card .vl{font-size:1.8rem;font-weight:700}
.stitle{display:flex;align-items:center;gap:10px;margin:30px 0 15px;font-size:1.4rem;font-weight:700}
.rec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px;margin-bottom:30px}
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
.mets{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.met{background:var(--bg);padding:8px 12px;border-radius:8px}
.met .ml{font-size:.7rem;color:var(--txt2)}
.met .mv{font-size:1rem;font-weight:600}
.mv.pos{color:var(--grn)}.mv.neg{color:var(--red)}
.risk-badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.8rem;font-weight:700}
.sbar{margin-top:12px;height:6px;background:var(--bg);border-radius:3px;overflow:hidden}
.sbar-f{height:100%;border-radius:3px;transition:width .8s}
.sbar-l{display:flex;justify-content:space-between;margin-top:4px;font-size:.75rem;color:var(--txt2)}
.sbd{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;margin-top:8px;text-align:center}
.sbd .sl{font-size:.6rem;color:var(--txt2)}.sbd .sv{font-size:.7rem;font-weight:600}
.prob{font-size:.85rem;padding:6px 12px;border-radius:8px;text-align:center;margin:8px 0;font-weight:600}
.prob.high{background:rgba(0,214,143,.12);color:var(--grn)}
.prob.mid{background:rgba(255,165,2,.12);color:var(--org)}
.prob.low{background:rgba(255,71,87,.12);color:var(--red)}
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
.info-box{background:var(--bg);border:1px solid var(--brd);border-radius:12px;padding:20px;margin:20px 0}
.info-box h3{color:var(--accl);margin-bottom:12px;font-size:1rem}
.info-box p{color:var(--txt2);font-size:.85rem;line-height:1.6;margin-bottom:8px}
.info-box strong{color:var(--txt)}
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
<form action="/analyze" method="POST" class="inp-g" onsubmit="showLoad('TEFAS verileri çekiliyor... 15-20 saniye sürebilir')">
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
<div class="sum-card"><div class="lb">İşlem Gören Fon</div><div class="vl" style="color:var(--acc)">{{ total }}</div></div>
<div class="sum-card"><div class="lb">Mevduat Üstü Fon</div><div class="vl" style="color:var(--grn)">{{ above }}</div></div>
</div>

<div class="info-box">
<h3>📌 Bu Puan Ne Demek?</h3>
<p><strong>Puan (0-100):</strong> Fonun gelecek ay mevduatı geçme olasılığını ve güvenilirliğini gösterir. Yüksek puan = hem mevduatı geçme ihtimali yüksek hem de riski düşük/öngörülebilir.</p>
<p><strong>Risk (1-7):</strong> TEFAS'ın kendi risk skalası. 1 = Para Piyasası (neredeyse mevduat kadar güvenli), 7 = Serbest fon (yüksek risk, yüksek dalgalanma).</p>
<p><strong>Geçme Olasılığı:</strong> Son 1, 3 ve 6 aylık dönemlerde kaçında mevduatı geçtiğine ve risk seviyesine göre tahmin edilir.</p>
<p><strong>Beklenen Aylık:</strong> Geçmiş getirilerin %30-40 güvenlik indirimiyle hesaplanmış konservatif tahmin. <u>Garanti değildir.</u></p>
</div>

<div class="stitle"><span style="font-size:1.6rem">⭐</span> Önerilen Fonlar</div>
<div class="rec-grid">
        {% for f in recommended %}
<div class="fc" onclick="location.href='https://www.tefas.gov.tr/tr/fon-ara?FonKod={{ f.code }}'">
<div class="rank">{{ loop.index }}</div>
<div class="fh">
<span class="fcode">{{ f.code }}</span>
<span class="fname">{{ f.name }}</span>
</div>
<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
<span class="risk-badge" style="background:{{ f.risk_color }}22;color:{{ f.risk_color }}">{{ f.risk_label }}</span>
</div>
<div class="mets">
<div class="met"><div class="ml">Fiyat</div><div class="mv">{{ "%.4f"|format(f.price) }}</div></div>
<div class="met"><div class="ml">Son 1 Ay (%)</div><div class="mv {{ 'pos' if f.r1m > 0 else 'neg' }}">{{ "%+.2f"|format(f.r1m) }}%</div></div>
<div class="met"><div class="ml">Son 3 Ay (%)</div><div class="mv {{ 'pos' if f.r3m > 0 else 'neg' }}">{{ "%+.2f"|format(f.r3m) }}%</div></div>
<div class="met"><div class="ml">Son 6 Ay (%)</div><div class="mv {{ 'pos' if f.r6m != None and f.r6m > 0 else ('neg' if f.r6m != None else '') }}">{{ "%+.2f"|format(f.r6m) if f.r6m != None else "—" }}{{ "%" if f.r6m != None else "" }}</div></div>
<div class="met"><div class="ml">Beklenen Aylık</div><div class="mv pos">{{ "+%.2f"|format(f.expected_monthly) }}%</div></div>
<div class="met"><div class="ml">Mevduat Aylık</div><div class="mv" style="color:var(--org)">{{ f.deposit_monthly }}%</div></div>
</div>
{% set prob_class = 'high' if 'Çok Yüksek' in f.probability or 'Yüksek' in f.probability else ('mid' if 'Orta' in f.probability else 'low') %}
<div class="prob {{ prob_class }}">🎯 Geçme Olasılığı: {{ f.probability }}</div>
{% set sc = "#00d68f" if f.score >= 70 else ("#4f8cff" if f.score >= 50 else ("#ffa502" if f.score >= 30 else "#ff4757")) %}
<div class="sbar"><div class="sbar-f" style="width:{{ f.score }}%;background:{{ sc }}"></div></div>
<div class="sbar-l"><span>Güvenilirlik Puanı</span><span style="color:{{ sc }};font-weight:700">{{ f.score }}/100</span></div>
<div class="sbd">
<div><div class="sl">Geçme Sıklığı</div><div class="sv">{{ f.s_beat }}/35</div></div>
<div><div class="sl">Risk Skoru</div><div class="sv">{{ f.s_risk }}/25</div></div>
<div><div class="sl">İstikrar</div><div class="sv">{{ f.s_stab }}/20</div></div>
<div><div class="sl">Fon Büyüklüğü</div><div class="sv">{{ f.s_size }}/10</div></div>
<div><div class="sl">Yatırımcı</div><div class="sv">{{ f.s_inv }}/10</div></div>
</div>
</div>
{% endfor %}
</div>

{% if not recommended %}
<p style="color:var(--txt2);padding:20px;text-align:center">Kriterlere uygun fon bulunamadı. Düşük faiz oranı deneyin.</p>
{% endif %}

<div class="disc">
⚠️ <strong>Önemli Uyarı:</strong> Bu uygulama geçmiş verilere dayalı olasılık hesabı yapmaktadır. <u>Yatırım tavsiyesi değildir.</u><br>
Geçmiş performans gelecekteki getirilerin garantisi değildir. Yüksek puanlı bir fon bile zarar edebilir.<br>
Karar verirken fonun <strong>risk seviyesini (1-7)</strong> mutlaka dikkate alın.
</div>

<div class="stitle"><span style="font-size:1.6rem">📋</span> Tüm Fonlar</div>
<div class="tc">
<div class="tabs">
<div class="tab act" onclick="showTab('above',this)">Mevduat Üstü ({{ above }})</div>
<div class="tab" onclick="showTab('all',this)">Tüm Fonlar ({{ total }})</div>
</div>
<div style="overflow-x:auto">
<table>
<thead><tr><th>#</th><th>Kod</th><th>Fon Adı</th><th>Risk</th><th>1A %</th><th>3A %</th><th>6A %</th><th>Beklenen</th><th>Geçme Ol.</th><th>Puan</th></tr></thead>
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
        const r6m = f.r6m !== null ? (f.r6m>0?'+':'')+f.r6m+'%' : '—';
        const r6c = f.r6m !== null ? (f.r6m>0?'var(--grn)':'var(--red)') : 'var(--txt2)';
        tb.innerHTML += `<tr onclick="window.open('https://www.tefas.gov.tr/tr/fon-ara?FonKod=${f.code}','_blank')">
            <td>${i+1}</td>
            <td><strong style="color:var(--accl)">${f.code}</strong></td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${f.name}</td>
            <td><span class="risk-badge" style="background:${f.risk_color}22;color:${f.risk_color}">${f.risk}/7</span></td>
            <td style="color:${f.r1m>0?'var(--grn)':'var(--red)'};font-weight:600">${f.r1m>0?'+':''}${f.r1m}%</td>
            <td style="color:${f.r3m>0?'var(--grn)':'var(--red)'};font-weight:600">${f.r3m>0?'+':''}${f.r3m}%</td>
            <td style="color:${r6c};font-weight:600">${r6m}</td>
            <td style="color:var(--grn);font-weight:600">+${f.expected_monthly}%</td>
            <td style="font-size:.8rem">${f.probability}</td>
            <td style="${sc};font-weight:700">${f.score}</td>
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
    dep_net = dep_decimal * 0.85
    dep_monthly = dep_net / 12

    try:
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
                '{% block content %}<div class="inp-sec"><h2 style="color:var(--red)">TEFAS\'tan veri alınamadı. Lütfen daha sonra tekrar deneyin.</h2><a href="/" class="btn" style="display:inline-block;margin-top:20px;text-decoration:none">← Geri Dön</a></div>{% endblock %}'
            ))

        actual_date_fmt = today_list[0].get("tarih", actual_date)
        logger.info(f"Bugün: {actual_date_fmt}, {len(today_list)} fon")

        actual_dt = datetime.strptime(actual_date_fmt, "%Y-%m-%d")

        logger.info("1 ay önce çekiliyor...")
        m1_date = subtract_months(actual_dt, 1)
        _, m1_list = find_nearest_day(m1_date.strftime("%Y%m%d"), offset_range=range(0, 5))
        logger.info(f"1a: {len(m1_list)} fon")

        logger.info("3 ay önce çekiliyor...")
        m3_date = subtract_months(actual_dt, 3)
        _, m3_list = find_nearest_day(m3_date.strftime("%Y%m%d"), offset_range=range(0, 5))
        logger.info(f"3a: {len(m3_list)} fon")

        logger.info("6 ay önce çekiliyor...")
        m6_date = subtract_months(actual_dt, 6)
        _, m6_list = find_nearest_day(m6_date.strftime("%Y%m%d"), offset_range=range(0, 7))
        logger.info(f"6a: {len(m6_list)} fon")

        scores = calculate_scores(today_list, m1_list, m3_list, m6_list, dep_net)

        above_list = [s for s in scores if s["beats"]]
        recommended = [s for s in above_list if s["score"] >= 30][:15]

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
            all_json=_json.dumps(scores[:50]),
            above_json=_json.dumps(above_list[:25]),
            fmt_size=fmt_size,
            fmt_num=fmt_num,
        )

    except Exception as e:
        logger.error(f"Hata: {e}", exc_info=True)
        return f"Analiz hatası: {e}", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
