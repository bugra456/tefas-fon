"""
TEFAS Fon Tavsiye Sistemi v2
Hedef: Mevduat faizini ANLAMLI şekilde geçebilecek fonları bulmak.
"""
import logging
from datetime import datetime, timedelta
import calendar
import time
import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, render_template_string, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# TEFAS API
# ═══════════════════════════════════════════════════════════════

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
DETAIL_URL = "https://www.tefas.gov.tr/api/funds/fonBilgiGetir"

API_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/tr/fon-verileri",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
            resp = requests.post(INFO_URL, json=api_body(target_date, target_date, kind),
                                headers=API_HEADERS, timeout=30)
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


# ═══════════════════════════════════════════════════════════════
# RİSK SEVİYESİ
# ═══════════════════════════════════════════════════════════════

def get_risk_level(fund_name):
    """Fon adından tahmini risk (fallback)."""
    n = fund_name.upper()
    if "PARA PİYASASI" in n or "PARA PIYASASI" in n:
        return 1
    if "BORÇLANMA" in n or "BORCLANMA" in n or "TAHVİL" in n or "TAHVIL" in n:
        return 2
    if "KAMU" in n and ("BORÇ" in n or "TAHVİL" in n or "TAHVIL" in n):
        return 2
    if "KARMA" in n:
        return 4
    if "DEĞİŞKEN" in n or "DEGISKEN" in n:
        return 4
    if "DÖVİZ" in n or "DOVIZ" in n or "DOLAR" in n or "EURO" in n:
        return 4
    if "ALTIN" in n:
        return 5
    if "GÜMÜŞ" in n or "GUMUS" in n:
        return 5
    if "EMTİA" in n or "EMTIA" in n:
        return 5
    if "HİSSE" in n or "HISSE" in n:
        return 6
    if "SERBEST" in n:
        return 7
    return 5


# fonKategori → risk (1-7)
CATEGORY_RISK = {
    "Para Piyasası Fonu": 1,
    "Kısa Vadeli Borçlanma Araçları Fonu": 2,
    "Kamu Borçlanma Araçları Fonu": 2,
    "Özel Sektör Borçlanma Araçları Fonu": 3,
    "Borçlanma Araçları Fonu": 3,
    "Eurobond Fonu": 3,
    "Değişken Fon": 4,
    "Karma Fon": 4,
    "Katılım Fonu": 4,
    "Altın Fonu": 5,
    "Kıymetli Madenler Fonu": 5,
    "Yabancı Fon Sepeti Fonu": 5,
    "Yabancı Hisse Senedi Fonu": 6,
    "Hisse Senedi Fonu": 6,
    "Hisse Senedi Yoğun Fon": 6,
    "Serbest Fon": 7,
    "Girişim Sermayesi Yatırım Fonları": 7,
    "Gayrimenkul Yatırım Fonları": 6,
}

RISK_LABELS = {
    1: ("1/7 - Çok Düşük", "#00d68f"),
    2: ("2/7 - Düşük", "#4f8cff"),
    3: ("3/7 - Orta Düşük", "#4f8cff"),
    4: ("4/7 - Orta", "#ffa502"),
    5: ("5/7 - Orta Yüksek", "#ffa502"),
    6: ("6/7 - Yüksek", "#ff4757"),
    7: ("7/7 - Çok Yüksek", "#ff4757"),
}


def _fetch_one_risk(code):
    """Tek fon için API'den risk çek."""
    try:
        resp = requests.post(DETAIL_URL, json={"fonKodu": code, "dil": "TR"},
                            headers=API_HEADERS, timeout=8)
        data = resp.json()
        if data.get("resultList"):
            cat = data["resultList"][0].get("fonKategori", "")
            name = data["resultList"][0].get("fonUnvan", "").upper()
            # Katılım Fonları alt-kategori
            if cat == "Katılım Fonu":
                if "PARA PİYASASI" in name or "PARA PIYASASI" in name:
                    return code, 1
                elif "ALTIN" in name:
                    return code, 5
                elif "HİSSE" in name or "HISSE" in name:
                    return code, 6
                return code, 4
            risk = CATEGORY_RISK.get(cat)
            if risk:
                return code, risk
    except Exception:
        pass
    return code, None


def fetch_real_risks(fund_codes, max_workers=10):
    """Paralel API çağrısı ile gerçek risk seviyelerini çek."""
    risk_map = {}
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one_risk, code): code for code in fund_codes}
            for future in as_completed(futures, timeout=20):
                code, risk = future.result()
                if risk is not None:
                    risk_map[code] = risk
    except Exception as e:
        logger.warning(f"Risk çekme kısmen başarısız: {e}")
    return risk_map


# ═══════════════════════════════════════════════════════════════
# MİNİMUM MEVDUAT FARKI (YAYILIM)
# ═══════════════════════════════════════════════════════════════

MIN_SPREAD = {
    1: 0.005,   # +0.50%
    2: 0.007,   # +0.70%
    3: 0.008,   # +0.80%
    4: 0.010,   # +1.00%
    5: 0.015,   # +1.50%
    6: 0.020,   # +2.00%
    7: 0.030,   # +3.00%
}

RISK_MULT = {1: 1.0, 2: 0.9, 3: 0.8, 4: 0.65, 5: 0.5, 6: 0.35, 7: 0.2}

# ═══════════════════════════════════════════════════════════════
# TEMEL ANALİZ — Faiz beklentisi bazlı puanlama
# ═══════════════════════════════════════════════════════════════

# Senaryo: {risk seviyesi: puan bonus/penalty}
OUTLOOK_BONUS = {
    "fall": {   # Faiz düşecek → tahvil fiyatları artar → borçlanma fonları lehte
        1: -5, 2: 15, 3: 12, 4: 5, 5: 0, 6: -3, 7: -5,
    },
    "stable": {  # Faiz sabit → nötr
        1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0,
    },
    "rise": {   # Faiz yükselecek → tahvil fiyatları düşer → borçlanma aleyhte
        1: 10, 2: -15, 3: -12, 4: -5, 5: 0, 6: 5, 7: 3,
    },
}

OUTLOOK_GUIDE = {
    "fall": {
        "title": "📉 Temel Analiz: Faiz Düşüş Ortamı",
        "summary": "TCMB kademeli faiz indirimi yapıyor. Bu ortamda tahvil fiyatları yükselir.",
        "fav": "Risk 2-3 (Borçlanma, Tahvil) fonlar",
        "avoid": "Risk 1 (Para Piyasası) — mevduat faizi de düşecek, avantaj azalır",
        "logic": "Faizler düşünce eski tahviller (yüksek kuponlu) değer kazanır. Borçlanma fonlarının portföyündeki tahviller değerlenecek.",
    },
    "stable": {
        "title": "➡️ Temel Analiz: Nötr Ortam",
        "summary": "TCMB faizleri sabit tutuyor. Temel analizin ek etkisi sınırlı.",
        "fav": "Tutarlı performans gösteren fonlar (teknik analiz ağırlıklı)",
        "avoid": "—",
        "logic": "Faiz değişmediği için tahvil fiyatlarında büyük hareket beklenmez. Geçmiş tutarlılık daha önemli.",
    },
    "rise": {
        "title": "📈 Temel Analiz: Faiz Yükseliş Ortamı",
        "summary": "TCMB faiz artırıyor. Bu ortamda tahvil fiyatları düşer.",
        "fav": "Risk 1 (Para Piyasası) — kısa vadeli, faiz artışından korunaklı",
        "avoid": "Risk 2-3 (Borçlanma) — tahvil fiyatları düşebilir, zarar riski var",
        "logic": "Faizler yükselince piyasada yeni tahviller daha yüksek kuponlu çıkar, eski tahviller değer kaybeder. Uzun vadeli borçlanma fonları olumsuz etkilenir.",
    },
}


# ═══════════════════════════════════════════════════════════════
# SKORLAMA VE FİLTRELEME
# ═══════════════════════════════════════════════════════════════

def calculate_scores(today_list, m1_list, m3_list, m6_list, deposit_annual_pct):
    deposit_monthly = (deposit_annual_pct / 100 * 0.85) / 12

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

        avg_1m = ret_1m
        avg_3m = ret_3m / 3 if ret_3m else None
        avg_6m = ret_6m / 6 if ret_6m else None

        # Beklenen aylık getiri
        if avg_6m is not None:
            expected = avg_6m * 0.50 + avg_3m * 0.30 + avg_1m * 0.20
        elif avg_3m is not None:
            expected = avg_3m * 0.60 + avg_1m * 0.40
        else:
            expected = avg_1m

        risk = get_risk_level(name)
        risk_label, risk_color = RISK_LABELS.get(risk, ("?/7", "#fff"))

        spread = expected - deposit_monthly
        spread_pct = round(spread * 100, 2)

        min_spread = MIN_SPREAD.get(risk, 0.01)
        passes = spread >= min_spread

        beat_1m = ret_1m > deposit_monthly
        beat_3m_avg = (ret_3m / 3) > deposit_monthly if ret_3m else False
        beat_6m_avg = (ret_6m / 6) > deposit_monthly if ret_6m else False
        beats_count = sum([beat_1m, beat_3m_avg, beat_6m_avg])

        # Puan hesabı
        risk_mult = RISK_MULT.get(risk, 0.5)
        s_spread = min(max(spread_pct, 0) * risk_mult * 15, 40)

        if beats_count == 3:
            s_cons = 25
        elif beats_count == 2:
            s_cons = 18
        elif beats_count == 1:
            s_cons = 8
        else:
            s_cons = 0

        if avg_6m is not None and avg_3m is not None:
            returns = [avg_1m, avg_3m, avg_6m]
            avg_ret = sum(returns) / len(returns)
            if avg_ret > 0 and len(returns) > 1:
                std = statistics.stdev(returns)
                cv = std / avg_ret
                stability = max(0, 1 - min(cv, 1))
                s_stab = round(stability * 15)
            else:
                s_stab = 0
        elif avg_3m is not None:
            diff = abs(avg_1m - avg_3m) / max(abs(avg_3m), 0.001)
            s_stab = round(max(0, 1 - min(diff, 1)) * 15)
        else:
            s_stab = 3

        if size > 5e9:
            s_size = 12
        elif size > 1e9:
            s_size = 10
        elif size > 500e6:
            s_size = 7
        elif size > 100e6:
            s_size = 4
        else:
            s_size = 2

        if inv_now > 50000:
            s_inv = 8
        elif inv_now > 10000:
            s_inv = 6
        elif inv_now > 1000:
            s_inv = 4
        else:
            s_inv = 1

        total = round(s_spread + s_cons + s_stab + s_size + s_inv, 1)

        # Geçme olasılığı
        if beats_count == 3 and spread_pct >= 1.5:
            prob = "Çok Yüksek (%85+)"
        elif beats_count == 3 and spread_pct >= 0.7:
            prob = "Yüksek (%75-85)"
        elif beats_count >= 2 and spread_pct >= 1.0:
            prob = "Yüksek (%70-80)"
        elif beats_count >= 2 and spread_pct >= 0.5:
            prob = "Orta (%55-70)"
        elif beats_count >= 1 and spread_pct >= 0.5:
            prob = "Orta (%50-65)"
        elif beats_count >= 1:
            prob = "Düşük (%35-50)"
        else:
            prob = "Çok Düşük (%20-35)"

        results.append({
            "code": code, "name": name, "price": price_now,
            "size": size, "investors": inv_now,
            "risk": risk, "risk_label": risk_label, "risk_color": risk_color,
            "r1m": round(ret_1m * 100, 2),
            "r3m": round(ret_3m * 100, 2),
            "r6m": round(ret_6m * 100, 2) if ret_6m is not None else None,
            "beat_1m": beat_1m, "beat_3m": beat_3m_avg, "beat_6m": beat_6m_avg,
            "beats_count": beats_count,
            "passes": passes,
            "score": total,
            "s_spread": round(s_spread, 1),
            "s_cons": s_cons,
            "s_stab": s_stab,
            "s_size": s_size,
            "s_inv": s_inv,
            "probability": prob,
            "expected_monthly": round(expected * 100, 2),
            "deposit_monthly": round(deposit_monthly * 100, 2),
            "spread_pct": spread_pct,
            "min_required": round((deposit_monthly + min_spread) * 100, 2),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════
# HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════

HTML_BASE = r"""
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TEFAS Fon Tavsiye</title>
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
.inp-sec h2{color:var(accl);margin-bottom:20px;font-size:1.2rem}
.inp-g{display:flex;align-items:center;justify-content:center;gap:15px;flex-wrap:wrap}
.inp-g input{background:var(--bg);border:2px solid var(--brd);border-radius:10px;
padding:12px 18px;color:var(--txt);font-size:1.3rem;width:180px;text-align:center}
.inp-g input:focus{outline:none;border-color:var(--acc)}
.btn{background:linear-gradient(135deg,var(--acc),#6366f1);color:#fff;border:none;
padding:14px 40px;border-radius:12px;font-size:1.1rem;font-weight:600;cursor:pointer;
box-shadow:0 4px 15px rgba(79,140,255,.3);transition:all .3s}
.btn:hover{transform:translateY(-2px);box-shadow:0 6px 25px rgba(79,140,255,.4)}
.back-link{display:inline-block;color:var(--accl);text-decoration:none;margin-bottom:15px;
font-size:1rem;padding:8px 16px;border-radius:8px;border:1px solid var(--brd);transition:.3s}
.back-link:hover{background:var(--card);border-color:var(--acc)}
.sum-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin:20px 0}
.sum-card{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:20px;text-align:center}
.sum-card .lb{font-size:.85rem;color:var(--txt2);margin-bottom:6px}
.sum-card .vl{font-size:1.8rem;font-weight:700}
.stitle{display:flex;align-items:center;gap:10px;margin:30px 0 15px;font-size:1.4rem;font-weight:700}
.info-box{background:var(--bg);border:1px solid var(--brd);border-radius:12px;padding:20px;margin:20px 0}
.info-box h3{color:var(--accl);margin-bottom:12px;font-size:1rem}
.info-box p{color:var(--txt2);font-size:.85rem;line-height:1.6;margin-bottom:6px}
.info-box strong{color:var(--txt)}
.info-box table{width:100%;border-collapse:collapse;margin:10px 0}
.info-box td{padding:5px 10px;font-size:.85rem;border-bottom:1px solid var(--brd)}
.no-result{background:var(--card);border:1px solid var(--org);border-radius:14px;padding:30px;
margin:20px 0;text-align:center}
.no-result h3{color:var(--org);margin-bottom:12px;font-size:1.2rem}
.no-result p{color:var(--txt2);line-height:1.6;margin-bottom:8px}
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
.risk-badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.8rem;font-weight:700}
.spread-box{background:linear-gradient(135deg,rgba(0,214,143,.1),rgba(0,214,143,.05));
border:1px solid rgba(0,214,143,.3);border-radius:10px;padding:12px;margin:10px 0;text-align:center}
.spread-box .sv{font-size:1.8rem;font-weight:800;color:var(--grn)}
.spread-box .sl{font-size:.75rem;color:var(--txt2);margin-top:2px}
.cmp-row{display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg);
border-radius:8px;margin:8px 0;font-size:.9rem}
.mets{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:8px}
.met{background:var(--bg);padding:6px 10px;border-radius:6px;text-align:center}
.met .ml{font-size:.65rem;color:var(--txt2)}
.met .mv{font-size:.9rem;font-weight:600}
.mv.pos{color:var(--grn)}.mv.neg{color:var(--red)}
.beat-row{display:flex;gap:8px;margin:8px 0;font-size:.75rem}
.beat-item{padding:3px 8px;border-radius:4px;font-weight:600}
.beat-item.ok{background:rgba(0,214,143,.12);color:var(--grn)}
.beat-item.no{background:rgba(255,71,87,.12);color:var(--red)}
.prob{font-size:.85rem;padding:6px 12px;border-radius:8px;text-align:center;margin:8px 0;font-weight:600}
.prob.high{background:rgba(0,214,143,.12);color:var(--grn)}
.prob.mid{background:rgba(255,165,2,.12);color:var(--org)}
.prob.low{background:rgba(255,71,87,.12);color:var(--red)}
.sbar{margin-top:10px;height:6px;background:var(--bg);border-radius:3px;overflow:hidden}
.sbar-f{height:100%;border-radius:3px;transition:width .8s}
.sbar-l{display:flex;justify-content:space-between;margin-top:4px;font-size:.75rem;color:var(--txt2)}
.tc{background:var(--card);border:1px solid var(--brd);border-radius:14px;overflow:hidden;margin:20px 0}
.tabs{display:flex;border-bottom:1px solid var(--brd)}
.tab{padding:12px 24px;cursor:pointer;color:var(--txt2);font-weight:500;
border-bottom:2px solid transparent;transition:.3s;font-size:.9rem}
.tab.act{color:var(--accl);border-bottom-color:var(--acc);background:rgba(79,140,255,.05)}
.tab:hover{color:var(--txt)}
table{width:100%;border-collapse:collapse}
table th{background:var(--bg2);padding:10px 14px;text-align:left;font-size:.75rem;
color:var(--txt2);text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
table td{padding:10px 14px;border-bottom:1px solid var(--brd);font-size:.85rem;white-space:nowrap}
table tbody tr:hover{background:var(--cardh);cursor:pointer}
.disc{background:rgba(255,165,2,.08);border:1px solid rgba(255,165,2,.2);border-radius:10px;
padding:15px;margin:20px 0;font-size:.8rem;color:var(--org);text-align:center;line-height:1.6}
.loading{display:none;position:fixed;inset:0;background:rgba(10,14,39,.9);z-index:200;
justify-content:center;align-items:center;flex-direction:column;gap:20px}
.loading.act{display:flex}
.spin{width:50px;height:50px;border:4px solid var(--brd);border-top-color:var(--acc);
border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media(max-width:768px){.ctn{padding:10px}.hdr h1{font-size:1.5rem}
.rec-grid{grid-template-columns:1fr}.inp-g{flex-direction:column}
.inp-g input{width:100%}.btn{width:100%}table{font-size:.75rem}
table th,table td{padding:6px 8px}}
.rfilter{background:var(--bg);border:1px solid var(--brd);color:var(--txt2);
padding:6px 16px;border-radius:20px;font-size:.85rem;cursor:pointer;transition:.3s;font-weight:500}
.rfilter:hover{border-color:var(--acc);color:var(--txt)}
.rfilter.act{background:var(--acc);color:#fff;border-color:var(--acc)}
</style>
</head>
<body>
<div class="ctn">
<div class="hdr">
<h1>📊 TEFAS Fon Tavsiye</h1>
<p>Mevduat faizini anlamlı şekilde geçebilecek fonları bulun</p>
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
</script>
</body>
</html>
"""

# ── ANA SAYFA ──

HOME_PAGE = HTML_BASE.replace(r"{% block content %}{% endblock %}", r"""
{% block content %}
<div class="inp-sec">
<h2>🏦 Yıllık Brüt Mevduat Faiz Oranınız</h2>
<form action="/analyze" method="POST" class="inp-g"
      onsubmit="showLoad('TEFAS verileri çekiliyor... 15-20 saniye sürebilir')">
<label>Yıllık Brüt Faiz:</label>
<input type="number" name="rate" value="40" min="1" max="200" step="0.5" required>
<label>%</label>
<button type="submit" class="btn">🔍 Fonları Analiz Et</button>
</form>
<p style="color:var(--txt2);font-size:.8rem;margin-top:12px">
Stopaj (%15) otomatik düşülür → Net aylık mevduat getirisi hesaplanır.<br>
<strong>Örnek:</strong> %40 brüt → %34 net yıllık → <strong>aylık %2.83 net</strong> mevduat getirisi.
</p>
</div>

<div class="inp-sec" style="margin-top:10px">
<h2>📉 TCMB Faiz Beklentiniz (Temel Analiz)</h2>
<p style="color:var(--txt2);font-size:.85rem;margin-bottom:15px">
Merkez Bankası'nın önümüzdeki dönemde ne yapacağını düşünüyorsunuz?<br>
<strong>Neden önemli?</strong> Faizler düşünce tahvil fiyatları yükselir → Borçlanma fonları kazanır.
Faizler yükselince tam tersi.
</p>
<form action="/analyze" method="POST" class="inp-g" style="flex-direction:column;gap:12px"
      onsubmit="showLoad('Analiz yapılıyor...')">
<div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:center">
<input type="radio" name="outlook" value="fall" id="o_fall" style="display:none">
<label for="o_fall" class="outlook-btn" onclick="selectOutlook(this,'fall')" style="cursor:pointer;background:var(--bg);border:2px solid var(--brd);border-radius:12px;padding:14px 20px;text-align:center;transition:.3s;min-width:200px;display:block">
<div style="font-size:1.5rem">📉</div>
<div style="font-weight:700;color:var(--grn)">Faiz Düşecek</div>
<div style="font-size:.75rem;color:var(--txt2);margin-top:4px">Kademeli faiz indirimi<br><strong style="color:var(--grn)">→ Tahvil fonları lehte</strong></div>
</label>
<input type="radio" name="outlook" value="stable" id="o_stable" style="display:none" checked>
<label for="o_stable" class="outlook-btn" onclick="selectOutlook(this,'stable')" style="cursor:pointer;background:var(--bg);border:2px solid var(--acc);border-radius:12px;padding:14px 20px;text-align:center;transition:.3s;min-width:200px;display:block">
<div style="font-size:1.5rem">➡️</div>
<div style="font-weight:700;color:var(--acc)">Faiz Sabit Kalacak</div>
<div style="font-size:.75rem;color:var(--txt2);margin-top:4px">Faiz değişikliği beklenmiyor<br><strong style="color:var(--acc)">→ Nötr ortam</strong></div>
</label>
<input type="radio" name="outlook" value="rise" id="o_rise" style="display:none">
<label for="o_rise" class="outlook-btn" onclick="selectOutlook(this,'rise')" style="cursor:pointer;background:var(--bg);border:2px solid var(--brd);border-radius:12px;padding:14px 20px;text-align:center;transition:.3s;min-width:200px;display:block">
<div style="font-size:1.5rem">📈</div>
<div style="font-weight:700;color:var(--red)">Faiz Yükselecek</div>
<div style="font-size:.75rem;color:var(--txt2);margin-top:4px">Faiz artışı bekleniyor<br><strong style="color:var(--red)">→ Tahvil fonları aleyhte</strong></div>
</label>
</div>
<input type="number" name="rate" id="hidden_rate" value="40" style="display:none">
<button type="submit" class="btn" style="margin-top:8px">🔍 Temel Analiz ile Tara</button>
</form>
</div>

<script>
function selectOutlook(el, val) {
    document.querySelectorAll('.outlook-btn').forEach(b => b.style.borderColor = 'var(--brd)');
    el.style.borderColor = 'var(--acc)';
}
// Üst formdaki rate değerini alt formdaki gizli inputa da kopyala
document.querySelector('input[name="rate"]')?.addEventListener('input', function() {
    document.getElementById('hidden_rate').value = this.value;
});
</script>
{% endblock %}
""")

# ── SONUÇ SAYFASI ──

RESULTS_PAGE = HTML_BASE.replace(r"{% block content %}{% endblock %}", r"""
{% block content %}
<a href="/" class="back-link">← Yeni Analiz</a>

<div class="sum-row">
<div class="sum-card"><div class="lb">Analiz Tarihi</div><div class="vl" style="font-size:1.2rem;color:var(--acc)">{{ date }}</div></div>
<div class="sum-card"><div class="lb">Mevduat Aylık Net</div><div class="vl" style="color:var(--org)">{{ dep_m }}%</div></div>
<div class="sum-card"><div class="lb">İşlem Gören Fon</div><div class="vl" style="color:var(--acc)">{{ total }}</div></div>
<div class="sum-card"><div class="lb">Mevduatı Geçen</div><div class="vl" style="color:var(--grn)">{{ above_count }}</div></div>
<div class="sum-card"><div class="lb">Önerilen Fon</div><div class="vl" style="color:var(--grn)">{{ rec_count }}</div></div>
</div>

<div class="info-box">
<h3>📌 Öneri Kriterleri — Risk seviyesine göre minimum fark</h3>

{% if outlook != 'stable' %}
<div style="background:rgba(79,140,255,.08);border:1px solid rgba(79,140,255,.3);border-radius:10px;padding:15px;margin-bottom:15px">
<h4 style="color:var(--acc);margin-bottom:8px">{{ guide.title }}</h4>
<p style="color:var(--txt);font-size:.9rem;margin-bottom:8px">{{ guide.summary }}</p>
<table style="margin:8px 0">
<tr><td style="color:var(--grn);font-weight:600">✓ Lehte</td><td>{{ guide.fav }}</td></tr>
<tr><td style="color:var(--red);font-weight:600">✗ Dikkat</td><td>{{ guide.avoid }}</td></tr>
</table>
<p style="color:var(--txt2);font-size:.8rem;border-top:1px solid var(--brd);padding-top:8px">
💡 <strong>Mantık:</strong> {{ guide.logic }}
</p>
</div>
{% endif %}
<p>Mevduatınız aylık <strong>{{ dep_m }}%</strong> getiriyor. Bir fonun önerilebilmesi için
beklenen aylık getirisi şu minimumları geçmelidir:</p>
<table>
<tr><td style="color:#00d68f">Risk 1 (Para Piyasası)</td><td>en az <strong>{{ min_r1 }}%</strong> (+0.50% fark)</td>
    <td style="color:var(--txt2)">Neredeyse mevduat kadar güvenli</td></tr>
<tr><td style="color:#4f8cff">Risk 2 (Borçlanma)</td><td>en az <strong>{{ min_r2 }}%</strong> (+0.70% fark)</td>
    <td style="color:var(--txt2)">Düşük risk, tahvil ağırlıklı</td></tr>
<tr><td style="color:#ffa502">Risk 4 (Karma/Değişken)</td><td>en az <strong>{{ min_r4 }}%</strong> (+1.00% fark)</td>
    <td style="color:var(--txt2)">Orta risk</td></tr>
<tr><td style="color:#ff4757">Risk 6 (Hisse)</td><td>en az <strong>{{ min_r6 }}%</strong> (+2.00% fark)</td>
    <td style="color:var(--txt2)">Yüksek risk, hisse senedi ağırlıklı</td></tr>
</table>
<p style="margin-top:8px">Böylece sana %3.01 mevduata karşı %3.02 getiren bir fon <strong>önerilmez</strong>.
Geçerli olmak için riskine göre anlamlı bir fark koymalıdır.</p>
</div>

{% if recommended %}
<div class="stitle"><span style="font-size:1.6rem">⭐</span> Önerilen Fonlar ({{ rec_count }})</div>
<div class="rec-grid">
{% for f in recommended %}
<div class="fc" onclick="window.open('https://www.tefas.gov.tr/tr/fon-detayli-analiz/{{ f.code }}','_blank')">
<div class="rank">{{ loop.index }}</div>
<div class="fh">
<span class="fcode">{{ f.code }}</span>
<span class="fname">{{ f.name }}</span>
</div>
<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
<span class="risk-badge" style="background:{{ f.risk_color }}22;color:{{ f.risk_color }}">{{ f.risk_label }}</span>
</div>

<div class="spread-box">
<div class="sv">+{{ f.spread_pct }}%</div>
<div class="sl">MEVDUAT FARKI (aylık)</div>
</div>

<div class="cmp-row">
<span>Beklenen: <strong style="color:var(--grn)">{{ f.expected_monthly }}%</strong></span>
<span>Mevduat: <strong style="color:var(--org)">{{ f.deposit_monthly }}%</strong></span>
</div>

<div class="mets">
<div class="met"><div class="ml">Son 1 Ay</div><div class="mv {{ 'pos' if f.r1m > 0 else 'neg' }}">{{ "%+.2f"|format(f.r1m) }}%</div></div>
<div class="met"><div class="ml">Son 3 Ay</div><div class="mv {{ 'pos' if f.r3m > 0 else 'neg' }}">{{ "%+.2f"|format(f.r3m) }}%</div></div>
<div class="met"><div class="ml">Son 6 Ay</div><div class="mv {{ 'pos' if f.r6m and f.r6m > 0 else ('neg' if f.r6m else '') }}">{{ "%+.2f"|format(f.r6m) if f.r6m is not none else "—" }}</div></div>
</div>

<div class="beat-row">
<span class="beat-item {{ 'ok' if f.beat_1m else 'no' }}">{{ "✓" if f.beat_1m else "✗" }} 1A</span>
<span class="beat-item {{ 'ok' if f.beat_3m else 'no' }}">{{ "✓" if f.beat_3m else "✗" }} 3A</span>
<span class="beat-item {{ 'ok' if f.beat_6m else 'no' }}">{{ "✓" if f.beat_6m else "✗" }} 6A</span>
<span style="color:var(--txt2);margin-left:auto">{{ f.beats_count }}/3 dönem geçti</span>
</div>

{% set prob_class = 'high' if 'Çok Yüksek' in f.probability or 'Yüksek' in f.probability else ('mid' if 'Orta' in f.probability else 'low') %}
<div class="prob {{ prob_class }}">🎯 Sonraki ay geçme olasılığı: {{ f.probability }}</div>

{% set sc = "#00d68f" if f.score >= 65 else ("#4f8cff" if f.score >= 45 else ("#ffa502" if f.score >= 25 else "#ff4757")) %}
<div class="sbar"><div class="sbar-f" style="width:{{ f.score }}%;background:{{ sc }}"></div></div>
<div class="sbar-l"><span>Güvenilirlik Puanı</span><span style="color:{{ sc }};font-weight:700">{{ f.score }}/100</span></div>
</div>
{% endfor %}
</div>
{% else %}
<div class="no-result">
<h3>😕 Mevduatınızı anlamlı şekilde geçen fon bulunamadı</h3>
<p>
Mevduatınız aylık <strong>{{ dep_m }}%</strong> net getiriyor.
Bu oran zaten yüksek — düşük riskli fonların bunu anlamlı şekilde geçmesi zor.
</p>
<p>
💡 <strong>Ne yapabilirsiniz?</strong><br>
• Mevduat faizinizden memnunsanız, kalabilirsiniz — zaten iyi bir oran.<br>
• Daha yüksek getiri istiyorsanız, Risk 4-6 fonları değerlendirin ama <strong>kayıp riskini</strong> unutmayın.<br>
• Aşağıda tüm fonları görebilir, hangi fon ne kadar getiri potansiyeline sahip inceleyebilirsiniz.
</p>
</div>
{% endif %}

<div class="disc">
⚠️ <strong>Uyarı:</strong> Geçmiş performans geleceğin garantisi değildir.
"Beklenen aylık" geçmiş ortalamalara dayalı gerçekçi bir tahmindir, kesin değildir.
Yüksek getiri = yüksek risk. Fonun risk seviyesini (1-7) mutlaka dikkate alın.
Bu uygulama yatırım tavsiyesi değildir.
</div>

<div class="info-box" style="border-color:var(--acc)">
<h3>🤔 Hangisini Seçmelisin? — Gerçekçi Rehber</h3>

<p><strong style="color:var(--org)">☝️ Önce bunu bil:</strong> Risk 1 (Para Piyasası) fonlar mevduat ile
<strong>neredeyse aynı</strong> getiriyi verir. Tek farkı günlük alım-satım esnekliği.
Mevduatı bırakıp Risk 1 fona geçmenin anlamı yok.</p>

<p><strong style="color:var(--grn)">✓ Neye bakmalısın?</strong></p>
<p>• <strong>3/3 dönem geçen fonları</strong> tercih et (✓✓✓) — tutarlılık en önemli gösterge</p>
<p>• <strong>Mevduat farkı +%1'den büyük olsun</strong> — %0.30 fark için risk almaya değmez</p>
<p>• <strong>Risk 3-4 fonlar</strong> — tahvil/karma fonlar, hisse senedi kadar dalgalanmaz</p>

<p><strong style="color:var(--red)">✗ Nelere dikkat et?</strong></p>
<p>• <strong>Faizler yükseliyorsa</strong> borçlanma fonları düşebilir — kötü zamanlama olabilir</p>
<p>• <strong>Sadece 1 ay iyi olanı seçme</strong> — 3/3 olmalı, yoksa şans eseri olabilir</p>
<p>• <strong>Beklenen aylık garanti değil</strong> — bir ay %4, diğer ay %1 olabilir</p>

<p><strong>💡 Strateji:</strong> Risk 3-4 filtresini seç → 3/3 geçenleri bul → farkı en yüksek olanı ara →
TEFAS'ta detayına bak → <strong>paramı kaybetmeyi göze alıyorum</strong> diyorsan yatırım yap.</p>
</div>

<div class="stitle"><span style="font-size:1.6rem">📋</span> Tüm Fonlar</div>
<div style="margin-bottom:12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
<span style="color:var(--txt2);font-size:.9rem">Risk Filtresi:</span>
<button class="rfilter act" onclick="filterRisk('all',this)">Tümü</button>
<button class="rfilter" onclick="filterRisk('low',this)">Düşük (1-2)</button>
<button class="rfilter" onclick="filterRisk('mid',this)">Orta (3-4)</button>
<button class="rfilter" onclick="filterRisk('high',this)">Yüksek (5-7)</button>
</div>
<div class="tc">
<div class="tabs">
<div class="tab act" onclick="showTab('above',this)">Mevduatı Geçen ({{ above_count }})</div>
<div class="tab" onclick="showTab('all',this)">Tümü ({{ total }})</div>
</div>
<div style="overflow-x:auto">
<table>
<thead><tr>
<th>#</th><th>Kod</th><th>Fon Adı</th><th>Risk</th>
<th>1A %</th><th>3A %</th><th>6A %</th>
<th>Beklenen</th><th>Mevduat</th><th>Fark</th>
<th>Geçme</th><th>Puan</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
</div>
</div>

<script>
const aboveFunds = {{ above_json|safe }};
const allFunds = {{ all_json|safe }};
let curRisk = 'all';

function riskMatch(risk) {
    if (curRisk === 'all') return true;
    if (curRisk === 'low') return risk <= 2;
    if (curRisk === 'mid') return risk >= 3 && risk <= 4;
    if (curRisk === 'high') return risk >= 5;
    return true;
}

function filterRisk(level, el) {
    curRisk = level;
    document.querySelectorAll('.rfilter').forEach(b => b.classList.remove('act'));
    el.classList.add('act');
    renderTable(curTab === 'above' ? aboveFunds : allFunds);
}

function renderTable(funds) {
    const tb = document.getElementById('tbody');
    tb.innerHTML = '';
    let idx = 0;
    funds.forEach(f => {
        if (!riskMatch(f.risk)) return;
        idx++;
        const sc = f.score>=65?'color:#00d68f':f.score>=45?'color:#4f8cff':f.score>=25?'color:#ffa502':'color:#ff4757';
        const spread_c = f.spread_pct>0?'var(--grn)':'var(--red)';
        const spread_s = f.spread_pct>0?'+':'';
        const r6m = f.r6m !== null ? (f.r6m>0?'+':'')+f.r6m+'%' : '\u2014';
        const r6c = f.r6m !== null ? (f.r6m>0?'var(--grn)':'var(--red)') : 'var(--txt2)';
        const star = f.passes ? '\u2b50' : '';
        tb.innerHTML += '<tr onclick="window.open(\'https://www.tefas.gov.tr/tr/fon-detayli-analiz/'+f.code+'\',\'_blank\')">' +
            '<td>'+star+idx+'</td>' +
            '<td><strong style="color:var(--accl)">'+f.code+'</strong></td>' +
            '<td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+f.name+'</td>' +
            '<td><span class="risk-badge" style="background:'+f.risk_color+'22;color:'+f.risk_color+'">'+f.risk+'/7</span></td>' +
            '<td style="color:'+(f.r1m>0?'var(--grn)':'var(--red)')+';font-weight:600">'+(f.r1m>0?'+':'')+f.r1m+'%</td>' +
            '<td style="color:'+(f.r3m>0?'var(--grn)':'var(--red)')+';font-weight:600">'+(f.r3m>0?'+':'')+f.r3m+'%</td>' +
            '<td style="color:'+r6c+';font-weight:600">'+r6m+'</td>' +
            '<td style="color:var(--grn);font-weight:600">'+f.expected_monthly+'%</td>' +
            '<td style="color:var(--org)">'+f.deposit_monthly+'%</td>' +
            '<td style="color:'+spread_c+';font-weight:700;font-size:.95rem">'+spread_s+f.spread_pct+'%</td>' +
            '<td style="font-size:.8rem">'+f.probability+'</td>' +
            '<td style="'+sc+';font-weight:700">'+f.score+'</td>' +
            '</tr>';
    });
    if (idx === 0) {
        tb.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--txt2);padding:30px">Bu risk seviyesinde fon bulunamadı. Filtreyi değiştirmeyi deneyin.</td></tr>';
    }
}

let curTab = 'above';
function showTab(tab, el) {
    curTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('act'));
    el.classList.add('act');
    renderTable(tab === 'above' ? aboveFunds : allFunds);
}
renderTable(aboveFunds);
</script>
{% endblock %}
""")

# ── HATA SAYFASI ──

ERROR_PAGE = HTML_BASE.replace(r"{% block content %}{% endblock %}", """
{% block content %}
<div class="inp-sec">
<h2 style="color:var(--red)">TEFAS'tan veri alınamadı</h2>
<p style="color:var(--txt2);margin:15px 0">TEFAS API şu an yanıt vermiyor. Lütfen birkaç dakika sonra tekrar deneyin.</p>
<a href="/" class="btn" style="display:inline-block;text-decoration:none">← Geri Dön</a>
</div>
{% endblock %}
""")


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def home():
    return render_template_string(HOME_PAGE)


@app.route("/analyze", methods=["POST"])
def analyze():
    rate = float(request.form.get("rate", 40))
    outlook = request.form.get("outlook", "stable")  # fall / stable / rise
    if outlook not in ("fall", "stable", "rise"):
        outlook = "stable"
    if rate <= 0 or rate > 200:
        return "Geçersiz faiz oranı", 400

    dep_monthly = (rate / 100 * 0.85) / 12

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
            return render_template_string(ERROR_PAGE)

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

        scores = calculate_scores(today_list, m1_list, m3_list, m6_list, rate)
        logger.info(f"Skorlanan: {len(scores)} fon")

        # Gerçek riskleri paralel çek (sadece top 25, 10 thread)
        try:
            top_codes = [s["code"] for s in scores[:25]]
            logger.info(f"Gerçek risk çekiliyor: {len(top_codes)} fon (paralel)...")
            real_risk_map = fetch_real_risks(top_codes, max_workers=10)
            logger.info(f"Gerçek risk alındı: {len(real_risk_map)} fon")

            for s in scores:
                real_risk = real_risk_map.get(s["code"])
                if real_risk is not None and real_risk != s["risk"]:
                    old_risk = s["risk"]
                    s["risk"] = real_risk
                    risk_label, risk_color = RISK_LABELS.get(real_risk, ("?/7", "#fff"))
                    s["risk_label"] = risk_label
                    s["risk_color"] = risk_color
                    min_spread = MIN_SPREAD.get(real_risk, 0.01)
                    s["min_required"] = round((s["deposit_monthly"] / 100 + min_spread) * 100, 2)
                    s["passes"] = s["spread_pct"] / 100 >= min_spread
                    risk_mult = RISK_MULT.get(real_risk, 0.5)
                    s["s_spread"] = round(min(max(s["spread_pct"], 0) * risk_mult * 15, 40), 1)
                    s["score"] = round(s["s_spread"] + s["s_cons"] + s["s_stab"] + s["s_size"] + s["s_inv"], 1)
                    logger.info(f"  {s['code']}: Risk {old_risk} -> {real_risk}")

            scores.sort(key=lambda x: x["score"], reverse=True)
        except Exception as e:
            logger.warning(f"Risk güncelleme atlandı: {e}")

        recommended = [s for s in scores if s["passes"] and s["risk"] >= 2][:20]
        above = [s for s in scores if s["spread_pct"] > 0]
        dep_pct = round(dep_monthly * 100, 2)

        # ── TEMEL ANALİZ BONUSU ──
        # Faiz beklentisine göre skor bonus/penalty uygula
        outlook_bonuses = OUTLOOK_BONUS.get(outlook, {})
        guide = OUTLOOK_GUIDE.get(outlook, OUTLOOK_GUIDE["stable"])
        for s in scores:
            bonus = outlook_bonuses.get(s["risk"], 0)
            s["outlook_bonus"] = bonus
            s["score_with_outlook"] = round(max(0, min(100, s["score"] + bonus)), 1)
        # Yeniden sırala (outlook dahil)
        scores.sort(key=lambda x: x["score_with_outlook"], reverse=True)
        recommended = [s for s in scores if s["passes"] and s["risk"] >= 2][:20]

        return render_template_string(RESULTS_PAGE,
            date=actual_date_fmt,
            dep_m=dep_pct,
            total=len(scores),
            above_count=len(above),
            rec_count=len(recommended),
            rate=rate,
            recommended=recommended,
            above_json=json.dumps(above[:40]),
            all_json=json.dumps(scores[:60]),
            min_r1=round((dep_monthly + 0.005) * 100, 2),
            min_r2=round((dep_monthly + 0.007) * 100, 2),
            min_r4=round((dep_monthly + 0.010) * 100, 2),
            min_r6=round((dep_monthly + 0.020) * 100, 2),
            outlook=outlook,
            guide=guide,
        )

    except Exception as e:
        logger.error(f"Hata: {e}", exc_info=True)
        return render_template_string(ERROR_PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
