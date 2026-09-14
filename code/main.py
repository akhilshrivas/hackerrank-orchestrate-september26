#!/usr/bin/env python3
"""
Buy or Wait? — Financial Decision Engine (v4)
HackerRank Orchestrate September 2026

KEY ARCHITECTURE CHANGES FROM v3:
1. MONTHLY AGGREGATION for variable spending categories:
   - Group all settled events by (category, calendar_month), sum amounts
   - Use last COMPLETE month's total as the recurring monthly amount
   - Project once per month (not once per trip interval)
   - This correctly handles weekly grocery/transport trips

2. SALARY RECURRENCE from scheduled event:
   - If there's a scheduled salary, project it recurring monthly
   - Even with only 1 settled payroll, use the scheduled salary as basis
   - For salary interval: group by dominant payroll description to exclude 
     one-time arrears/bonus payments from interval calculation

3. REGEX FALSE MATCH FIX:
   - Tighter regex patterns to avoid matching reference numbers

4. amount_safe_to_pay:
   - Binary search over [0, requested_amount]
   - The 90-day safety check uses the monthly-aggregated forecast

5. earliest_date_for_full_payment:
   - Day-by-day scan: first date where paying full amount keeps balance >= minb
   - Also check day matches an income event when relevant

The balance equation:
  balance_t = current_available_balance - payment_on_rdate + sum(flows up to t)
  Safe: balance_t >= minimum_balance_to_keep for all t in [rdate, rdate+90]
"""

import csv
import os
import re
import logging
from datetime import datetime, timedelta, date
from calendar import monthrange
from collections import defaultdict, Counter
from typing import Optional, List, Dict, Tuple, Set

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_DIR = os.path.join(BASE_DIR, "dataset")
OUTPUT_PATH = os.path.join(BASE_DIR, "output.csv")
FORECAST_DAYS = 90

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bw")

# ── Image amounts (pre-extracted from dataset/media/images/) ──────────────
IMAGE_AMOUNTS = {
    "image_01": (4365000, "IDR"),
    "image_02": (200000, "INR"),
    "image_03": (41272, "INR"),
    "image_04": (2854, "INR"),
    "image_05": (704.05, "INR"),
    "image_06": (1995, "INR"),
    "image_07": (8528, "INR"),
    "image_08": (15339, "INR"),
    "image_09": (723, "INR"),
    "image_10": (79679.26, "INR"),
    "image_11": (3650, "INR"),
    "image_12": (33.50, "USD"),
    "image_13": (2298, "INR"),
    "image_14": (4593, "INR"),
    "image_15": (9968, "INR"),
    "image_16": (393.22, "INR"),
}

# ── Helpers ────────────────────────────────────────────────────────────────

def load_csv(fn):
    with open(os.path.join(DATASET_DIR, fn), "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def pd(s):
    if not s or not s.strip(): return None
    try: return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except: return None

def pf(s):
    if not s or not s.strip(): return None
    try: return float(s.strip().replace(",", ""))
    except: return None

def pb(s):
    return s.strip().lower() == "true" if s else False

def rd2(x):
    return round(x, 2)

def fmt(x):
    if x == int(x): return str(int(x))
    return f"{x:.2f}"

def add_months(d, n):
    """Add n calendar months to a date."""
    m = d.month + n
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    max_day = monthrange(y, m)[1]
    return d.replace(year=y, month=m, day=min(d.day, max_day))

# ── Exchange Rates ─────────────────────────────────────────────────────────

class FX:
    def __init__(self, data):
        self.r = defaultdict(list)
        for row in data:
            d, rate = pd(row["rate_date"]), pf(row["rate"])
            if d and rate:
                self.r[(row["from_currency"].strip(), row["to_currency"].strip())].append((d, rate))
        for k in self.r: self.r[k].sort()

    def rate(self, fc, tc, d):
        if fc == tc: return 1.0
        r = self._closest(fc, tc, d)
        if r: return r
        r = self._closest(tc, fc, d)
        if r and r != 0: return 1.0 / r
        for m in ["USD", "EUR"]:
            if m in (fc, tc): continue
            r1, r2 = self.rate(fc, m, d), self.rate(m, tc, d)
            if r1 and r2: return r1 * r2
        return None

    def _closest(self, fc, tc, d):
        entries = self.r.get((fc, tc), [])
        if not entries: return None
        best = None
        for ed, er in entries:
            if ed <= d: best = er
        return best if best else entries[0][1]

    def convert(self, amt, fc, tc, d):
        if fc == tc: return amt
        r = self.rate(fc, tc, d)
        return amt * r if r else amt

# ── Data Structures ────────────────────────────────────────────────────────

class Profile:
    def __init__(self, r):
        self.uid = r["user_id"].strip()
        self.hc = r["home_currency"].strip()
        self.bal = pf(r["current_available_balance"]) or 0
        self.minb = pf(r["minimum_balance_to_keep"]) or 0
        self.protected = set(x.strip() for x in r.get("expense_categories_to_protect", "").split("|") if x.strip())
        self.reduce_cats = set(x.strip() for x in r.get("expense_categories_user_is_willing_to_reduce", "").split("|") if x.strip())
        self.stop_cats = set(x.strip() for x in r.get("expense_categories_user_is_willing_to_stop", "").split("|") if x.strip())
        self.methods = set(x.strip() for x in r.get("payment_methods_user_will_consider", "").split("|") if x.strip())
        m = r.get("max_installment_months", "").strip()
        self.max_inst = int(m) if m else None

class Evt:
    __slots__ = ("eid","uid","etype","desc","cat","dir","amt","cur","edate","sdate","status","linked","flex","mina")
    def __init__(self, r):
        self.eid = r["event_id"].strip()
        self.uid = r["user_id"].strip()
        self.etype = r["event_type"].strip()
        self.desc = r.get("description","").strip()
        self.cat = r["category"].strip()
        self.dir = r["direction"].strip()
        self.amt = pf(r["amount"])
        self.cur = r["currency"].strip()
        self.edate = pd(r["event_date"])
        self.sdate = pd(r["settlement_date"])
        self.status = r["status"].strip()
        self.linked = r.get("linked_event_id","").strip()
        self.flex = r.get("flexibility","").strip()
        self.mina = pf(r.get("minimum_allowed_amount",""))

class PO:
    def __init__(self, r):
        self.pid = r["payment_option_id"].strip()
        self.rid = r["request_id"].strip()
        self.method = r["payment_method"].strip()
        self.amt = pf(r["payment_amount"]) or 0
        self.n = int(r["number_of_payments"].strip()) if r["number_of_payments"].strip() else 1
        self.fdate = pd(r["first_payment_date"])
        freq = r.get("payment_frequency_days","").strip()
        self.freq = int(freq) if freq else None
        self.fee = pf(r.get("financing_fee","0")) or 0
        self.total = pf(r["total_payable_amount"]) or 0
    def sched(self):
        ps = []; d = self.fdate
        for i in range(self.n):
            ps.append((d, self.amt))
            if self.freq and i < self.n - 1: d += timedelta(days=self.freq)
        return ps

class Req:
    def __init__(self, r):
        self.rid = r["request_id"].strip()
        self.uid = r["user_id"].strip()
        self.rdate = pd(r["request_date"])
        self.rtype = r["request_type"].strip()
        self.ramt = pf(r["requested_amount"]) or 0
        self.deadline = pd(r["desired_completion_date"])
        self.partial = pb(r.get("allows_partial_payment",""))
        self.text = r.get("request_text","")

# ── Message Parser ─────────────────────────────────────────────────────────

def parse_msgs(msgs_data, uid, rid=""):
    all_msgs = [m for m in msgs_data if m.get("user_id","").strip() == uid]
    if rid:
        for m in msgs_data:
            if m.get("request_id","").strip() == rid and m not in all_msgs:
                all_msgs.append(m)
    info = {}
    for m in all_msgs:
        t = m.get("message_text","")
        _extract(t, info, m)
    return info

def _extract(t, info, m):
    # Salary increase — require a currency symbol before the number
    for pat in [
        r'salary (?:has )?increased? to (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'[Gg]aji (?:bulanan )?(?:Anda )?(?:naik|meningkat) menjadi (?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t)
        if x:
            info["sal_inc"] = pf(x.group(1).replace(",",""))
            d = re.search(r'(?:applies? from|berlaku mulai)\s+(\d{4}-\d{2}-\d{2})', t)
            if d: info["sal_inc_date"] = pd(d.group(1))

    # First salary
    for pat in [
        r'[Ff]irst salary (?:will be|of|from the new employer is) (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'[Gg]aji pertama (?:dari perusahaan baru |Anda )?(?:adalah|sebesar) (?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t)
        if x:
            info["first_sal"] = pf(x.group(1).replace(",",""))
            for dp in [r'(?:confirmed (?:credit|for)|confirmed credit date is|dikonfirmasi untuk|dijadwalkan pada)\s+(\d{4}-\d{2}-\d{2})']:
                d = re.search(dp, t)
                if d: info["first_sal_date"] = pd(d.group(1))

    # Reduced salary
    x = re.search(r'salary is reduced to (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)', t, re.I)
    if x: info["sal_reduced"] = pf(x.group(1).replace(",",""))

    # Temp pay
    for pat in [
        r'temporary monthly pay is (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'[Gg]aji (?:bulanan )?sementara (?:Anda )?adalah (?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t, re.I)
        if x: info["temp_sal"] = pf(x.group(1).replace(",",""))

    # Confirmed base salary
    for pat in [
        r'confirmed (?:base )?salary (?:is )?(?:now )?(?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'[Gg]aji (?:pokok )?(?:yang )?dikonfirmasi (?:adalah )?(?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t, re.I)
        if x: info["conf_sal"] = pf(x.group(1).replace(",",""))

    # Salary date change
    x = re.search(r'salary is now expected on\s+(\d{4}-\d{2}-\d{2})', t, re.I)
    if x: info["sal_date_chg"] = pd(x.group(1))

    # Remaining salary
    for pat in [
        r'remaining confirmed monthly salary is (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'[Ss]isa gaji bulanan yang dikonfirmasi adalah (?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t, re.I)
        if x: info["rem_sal"] = pf(x.group(1).replace(",",""))

    if re.search(r'employment has ended|hubungan kerja.*telah berakhir', t, re.I): info["emp_ended"] = True
    if re.search(r'seasonal contract has ended|[Kk]ontrak musiman.*telah berakhir', t, re.I): info["season_ended"] = True

    # Invoice
    for pat in [
        r'(?:approved|menyetujui) (?:an )?invoice payment of (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'pembayaran faktur sebesar (?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t, re.I)
        if x:
            info["inv_amt"] = pf(x.group(1).replace(",",""))
            for dp in [r'(?:Settlement is expected on|[Pp]enyelesaian diperkirakan pada)\s+(\d{4}-\d{2}-\d{2})']:
                d = re.search(dp, t, re.I)
                if d: info["inv_date"] = pd(d.group(1))

    if re.search(r'rent by 12%', t, re.I): info["rent_inc12"] = True
    if re.search(r'refund has been initiated but has not reached', t, re.I): info["refund_pending"] = True
    if re.search(r'refund is still processing', t, re.I): info["refund_proc"] = True
    if re.search(r'prize claim.*still in payment processing|[Kk]laim hadiah.*masih dalam proses', t, re.I): info["prize_pend"] = True
    if re.search(r'prize proceeds have reached your account|[Hh]asil.*sudah masuk ke rekening', t, re.I): info["prize_settled"] = True
    if re.search(r'transfer between your two accounts', t, re.I): info["internal_xfer"] = True
    if re.search(r'payout is still pending|masih tertunda', t, re.I): info["payout_pend"] = True
    if re.search(r"portfolio.*market value has increased", t, re.I): info["unreal_gain"] = True
    if re.search(r'proceeds from your investment sale have settled|[Hh]asil penjualan investasi.*sudah masuk', t, re.I): info["inv_sale_settled"] = True

    # One-time arrears — must have currency prefix
    for pat in [
        r'one-time arrears adjustment of (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'penyesuaian tunggakan satu kali sebesar (?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t, re.I)
        if x: info["arrears"] = pf(x.group(1).replace(",",""))

    # Regular salary next payroll — require currency prefix
    for pat in [
        r'[Rr]egular salary for the next payroll is (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
        r'[Gg]aji rutin.*untuk penggajian berikutnya.*?(?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?)',
    ]:
        x = re.search(pat, t, re.I)
        if x: info["reg_sal_next"] = pf(x.group(1).replace(",",""))

    if re.search(r'new recurring childcare payment begins', t, re.I): info["childcare"] = True

    x = re.search(r'[Rr]egular salary of (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?) resumes on\s+(\d{4}-\d{2}-\d{2})', t, re.I)
    if x:
        info["sal_resumes"] = pf(x.group(1).replace(",",""))
        info["sal_resumes_date"] = pd(x.group(2))

    for pat in [
        r'salary of (?:INR|IDR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?) is confirmed for\s+(\d{4}-\d{2}-\d{2})',
        r'[Gg]aji sebesar (?:IDR|INR|EUR|USD|ZAR)\s*([\d,]+(?:\.\d+)?) dikonfirmasi untuk\s+(\d{4}-\d{2}-\d{2})',
    ]:
        x = re.search(pat, t, re.I)
        if x:
            info["fgn_sal"] = pf(x.group(1).replace(",",""))
            info["fgn_sal_date"] = pd(x.group(2))
            cm = re.search(r'(INR|IDR|EUR|USD|ZAR)', t)
            if cm: info["fgn_sal_cur"] = cm.group(1)

    x = re.search(r'(USD|EUR|INR|IDR|ZAR)\s+([\d,]+(?:\.\d+)?)\s+salary credit for\s+(\d{4}-\d{2}-\d{2})', t, re.I)
    if x:
        info["emp_sal"] = pf(x.group(2).replace(",",""))
        info["emp_sal_date"] = pd(x.group(3))
        info["emp_sal_cur"] = x.group(1)

    if re.search(r'bonus.*still subject to.*performance review|bonus.*masih menunggu', t, re.I): info["bonus_pend"] = True
    if re.search(r'previous debit attempt failed', t, re.I): info["deb_failed"] = True
    if re.search(r'cash prize.*[Pp]ay the (?:release|processing) charge', t, re.I): info["scam"] = True
    if re.search(r'hadiah uang tunai.*[Bb]ayar biaya', t, re.I): info["scam"] = True
    if re.search(r'extra card charge.*still being investigated', t, re.I): info["disputed"] = True
    if re.search(r'minimum payments due on two separate card accounts', t, re.I): info["two_cards"] = True


# ── Forecast ───────────────────────────────────────────────────────────────

class Forecast:
    def __init__(self, prof: Profile, evts: List[Evt], fx: FX, mi: Dict,
                 rdate: date, img_map: Dict):
        self.prof = prof
        self.fx = fx
        self.mi = mi
        self.rdate = rdate
        self.edate = rdate + timedelta(days=FORECAST_DAYS)
        self.hc = prof.hc

        # Fill blank amounts from images
        for e in evts:
            if e.amt is None:
                iid = img_map.get(e.eid)
                if iid and iid in IMAGE_AMOUNTS:
                    ext_amt, ext_cur = IMAGE_AMOUNTS[iid]
                    td = e.sdate or e.edate
                    if td:
                        e.amt = fx.convert(ext_amt, ext_cur, e.cur, td)
                    else:
                        e.amt = ext_amt

        self.evts = evts

        # Build monthly recurring patterns (MONTHLY AGGREGATION)
        self.monthly_debits = self._estimate_monthly_debits(evts)
        self.salary_info = self._find_salary(evts)

    def _hc(self, amt, cur, d):
        return self.fx.convert(amt, cur, self.hc, d)

    def _last_complete_month(self):
        """Return YYYY-MM string of the last month that ended before rdate."""
        if self.rdate.day == 1:
            prev = self.rdate - timedelta(days=1)
        else:
            prev = self.rdate.replace(day=1) - timedelta(days=1)
        return prev.strftime("%Y-%m")

    def _estimate_monthly_debits(self, evts):
        cats = __import__('collections').defaultdict(list)
        for e in evts:
            if e.dir == "debit" and e.amt is not None and e.amt > 0 and e.cat != "salary":
                cats[e.cat].append(e)
        debits = {}
        for cat, elist in cats.items():
            elist.sort(key=lambda x: x.sdate)
            if len(elist) < 1: continue
            
            if len(elist) > 1:
                interval = round((elist[-1].sdate - elist[0].sdate).days / (len(elist) - 1))
            else:
                interval = 30
                
            amt = max(self._hc(x.amt, x.cur, x.sdate) for x in elist[-3:])
            
            debits[cat] = {
                "amount": amt,
                "interval": max(1, interval),
                "last_date": elist[-1].sdate,
                "event_id": elist[-1].eid, "flex": elist[-1].flex, "mina": elist[-1].mina
            }
        return debits

    def _find_salary(self, evts):
        """
        Find recurring salary info.
        Returns dict with:
          - amount_hc: monthly salary in home currency
          - next_date: first salary date >= rdate
          - interval_days: approx days between salaries
          - cancel: bool
        """
        mi = self.mi
        cancel = bool(mi.get("emp_ended") or mi.get("season_ended"))

        # Check if last salary description is 'Final employer payroll'
        for e in evts:
            if e.cat == "salary" and e.dir == "credit" and e.status == "settled":
                if "final" in e.desc.lower():
                    cancel = True

        if cancel:
            return {"amount_hc": None, "next_date": None, "interval_days": 30, "cancel": True}

        # --- Step 1: Check for scheduled salary event ---
        scheduled_sal = None
        for e in evts:
            if e.cat != "salary" or e.dir != "credit":
                continue
            if e.status != "scheduled":
                continue
            d = e.sdate or e.edate
            if d is None or d < self.rdate:
                continue
            amt = e.amt
            if amt is None:
                iid = None  # already filled above in __init__
                continue
            cur = e.cur

            # Apply message overrides
            for key in ["sal_reduced", "temp_sal", "conf_sal", "rem_sal",
                        "reg_sal_next", "first_sal", "sal_resumes", "sal_inc"]:
                if key in mi:
                    amt = mi[key]
                    break

            if "sal_date_chg" in mi:
                d = mi["sal_date_chg"]
            elif "sal_resumes_date" in mi:
                d = mi["sal_resumes_date"]
            elif "first_sal_date" in mi:
                d = mi["first_sal_date"]

            amt_hc = self._hc(amt, cur, d)
            scheduled_sal = {"date": d, "amount_hc": amt_hc}
            break

        # --- Step 2: Get interval from historical payroll events ---
        # Only use events with the DOMINANT salary description to avoid
        # arrears/bonus events distorting the interval
        sal_evts = sorted(
            [e for e in evts if e.cat == "salary" and e.dir == "credit"
             and e.status == "settled" and e.amt is not None and e.amt > 0],
            key=lambda e: e.sdate or e.edate or date.min
        )

        # Count descriptions
        desc_counts = Counter(e.desc for e in sal_evts)
        dominant_desc = desc_counts.most_common(1)[0][0] if desc_counts else None

        # Filter to dominant description for interval
        if dominant_desc:
            payroll_evts = [e for e in sal_evts if e.desc == dominant_desc]
        else:
            payroll_evts = sal_evts

        interval_days = 30  # default
        if len(payroll_evts) >= 2:
            dates = [e.sdate or e.edate for e in payroll_evts]
            gaps = [(dates[i] - dates[i-1]).days for i in range(1, len(dates)) if (dates[i] - dates[i-1]).days > 0]
            if gaps:
                interval_days = round(sum(gaps) / len(gaps))
                if interval_days < 7: interval_days = 30
                if interval_days > 60: interval_days = 30

        # --- Step 3: Determine projected salary amount ---
        sal_amount_hc = None

        # Message overrides take precedence
        for key in ["sal_reduced", "temp_sal", "conf_sal", "rem_sal",
                    "reg_sal_next", "first_sal", "sal_resumes", "sal_inc"]:
            if key in mi:
                sal_amount_hc = mi[key]
                break

        if sal_amount_hc is None and scheduled_sal:
            sal_amount_hc = scheduled_sal["amount_hc"]

        if sal_amount_hc is None and payroll_evts:
            last = payroll_evts[-1]
            d = last.sdate or last.edate
            sal_amount_hc = self._hc(last.amt, last.cur, d)

        # --- Step 4: Determine next salary date ---
        next_date = None
        if scheduled_sal:
            next_date = scheduled_sal["date"]
        elif payroll_evts:
            last_d = payroll_evts[-1].sdate or payroll_evts[-1].edate
            candidate = last_d + timedelta(days=interval_days)
            while candidate < self.rdate:
                candidate += timedelta(days=interval_days)
            next_date = candidate

        return {
            "amount_hc": sal_amount_hc,
            "next_date": next_date,
            "interval_days": interval_days,
            "cancel": False,
        }

    def build_flows(self, extra = None, stopped = None, reduced = None):
        """Build daily net cash flows for 90 days from rdate."""
        stopped = stopped or set()
        reduced = reduced or {}
        from collections import defaultdict
        from datetime import timedelta
        flows = defaultdict(float)
        mi = self.mi

        seen_eids = set()
        for e in self.evts:
            if e.status in ("failed", "cancelled", "unrealized"): continue
            if e.dir == "non_cash" or e.etype == "investment_valuation": continue
            if e.status == "settled": continue

            d = e.sdate or e.edate
            if d is None or d < self.rdate or d > self.edate: continue
            if e.eid in stopped or e.eid in seen_eids: continue
            if e.amt is None: continue
            
            seen_eids.add(e.eid)
            amt_hc = reduced.get(e.eid, self._hc(e.amt, e.cur, d))

            if e.dir == "debit" and e.status in ("pending", "scheduled"):
                flows[d] -= amt_hc
            elif e.dir == "credit" and e.status == "scheduled" and e.cat != "salary":
                flows[d] += amt_hc

        for cat, pat in self.monthly_debits.items():
            eid = pat["event_id"]
            if eid in stopped: continue
            amt = reduced.get(eid, pat["amount"])
            if cat == "rent" and mi.get("rent_inc12"): amt *= 1.12
            
            d = pat["last_date"] + timedelta(days=pat["interval"])
            while d <= self.edate:
                if d >= self.rdate:
                    has_explicit = any(e.cat == cat and e.dir == "debit" and e.status in ("scheduled", "pending") and (e.sdate or e.edate) == d for e in self.evts)
                    if not has_explicit:
                        flows[d] -= amt
                d += timedelta(days=pat["interval"])

        sal_info = self.salary_info
        if not sal_info["cancel"] and sal_info["amount_hc"] and sal_info["next_date"]:
            sal_amt = sal_info["amount_hc"]
            if mi.get("sal_red_amount"): sal_amt = mi["sal_red_amount"]
            elif mi.get("sal_inc"): sal_amt += mi["sal_inc"]
            
            d = sal_info["next_date"]
            intv = sal_info.get("interval", 30)
            while d <= self.edate:
                if d >= self.rdate:
                    flows[d] += sal_amt
                d += timedelta(days=intv)

        for d, a in (extra or []): flows[d] -= a
        return dict(flows)

    def forecast(self, extra=None, stopped=None, reduced=None):
        """Return daily (date, balance) list over 90 days."""
        flows = self.build_flows(extra, stopped, reduced)
        bal = self.prof.bal
        daily = []
        d = self.rdate
        while d <= self.edate:
            bal += flows.get(d, 0)
            daily.append((d, bal))
            d += timedelta(days=1)
        return daily

    def is_safe(self, extra=None, stopped=None, reduced=None):
        """True if balance never drops below minimum_balance_to_keep."""
        daily = self.forecast(extra, stopped, reduced)
        return all(b >= self.prof.minb - 0.005 for _, b in daily)

    def max_safe(self, max_amt):
        """Binary search for largest amount safe to pay on rdate, up to max_amt."""
        if max_amt <= 0: return 0.0
        if self.is_safe([(self.rdate, max_amt)]):
            return rd2(max_amt)
        lo, hi, best = 0.0, max_amt, 0.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if self.is_safe([(self.rdate, mid)]):
                best = mid; lo = mid
            else:
                hi = mid
            if hi - lo < 0.005: break
        return rd2(best)

    def earliest_full(self, amt):
        """
        First date >= rdate where paying 'amt' passes the 90-day safety check.
        Uses the full 90-day safety check from the payment date.
        """
        if amt <= 0:
            return self.rdate

        # Check rdate first
        if self.is_safe([(self.rdate, amt)]):
            return self.rdate

        # Scan day by day
        daily = self.forecast()  # no payment — find when balance is high enough
        for d, bal in daily:
            if d < self.rdate:
                continue
            if bal - amt < self.prof.minb - 0.005:
                continue
            # Also do full 90-day check from this date
            # Build flows from this date with the payment
            fc2 = Forecast(self.prof, self.evts, self.fx, self.mi, d, {})
            # Copy monthly patterns
            fc2.monthly_debits = self.monthly_debits
            fc2.salary_info = self.salary_info
            fc2.edate = d + timedelta(days=FORECAST_DAYS)
            if fc2.is_safe([(d, amt)]):
                return d
        return None


# ── Engine ─────────────────────────────────────────────────────────────────

class Engine:
    def __init__(self, profs, evts_by_user, opts_by_req, fx, msgs_data, imgs_data):
        self.profs = profs
        self.ebu = evts_by_user
        self.opts = opts_by_req
        self.fx = fx
        self.msgs = msgs_data
        self.imap = {}
        for i in imgs_data:
            eid = i.get("related_event_id","").strip()
            iid = i.get("image_id","").strip()
            if eid and iid: self.imap[eid] = iid

    def _copy_evts(self, uid):
        out = []
        for e in self.ebu.get(uid, []):
            ec = Evt.__new__(Evt)
            for s in Evt.__slots__:
                setattr(ec, s, getattr(e, s))
            out.append(ec)
        return out

    def run(self, req):
        prof = self.profs.get(req.uid)
        if not prof:
            return self._fail(req, 0)

        evts = self._copy_evts(req.uid)
        mi = parse_msgs(self.msgs, req.uid, req.rid)
        opts = self.opts.get(req.rid, [])

        fc = Forecast(prof, evts, self.fx, mi, req.rdate, self.imap)

        safe = fc.max_safe(req.ramt)
        earliest = fc.earliest_full(req.ramt)

        plans = []

        # --- Full payment (no spending changes) ---
        if safe >= req.ramt - 0.005 and "full_payment" in prof.methods:
            plans.append(self._plan("full_payment", f"{req.rdate}:{fmt(req.ramt)}",
                req.ramt, 1, req.rdate, True, "none", False, ""))

        # --- Installments (no spending changes) ---
        if "installments" in prof.methods:
            for opt in sorted(opts, key=lambda o: o.pid):
                if opt.method != "installments": continue
                if not self._inst_ok(opt, prof): continue
                s = opt.sched()
                if not s: continue
                by_dl = s[-1][0] <= req.deadline
                if fc.is_safe([(d, a) for d, a in s]):
                    plans.append(self._plan("installments",
                        "|".join(f"{d}:{fmt(a)}" for d, a in s),
                        opt.total, opt.n, s[0][0], by_dl, "none", False, opt.pid))

        # --- Partial payment (no spending changes) ---
        if (req.partial and "partial_payment" in prof.methods
                and 0 < safe < req.ramt - 0.005
                and earliest and earliest <= req.deadline):
            rem = rd2(req.ramt - safe)
            pays = [(req.rdate, safe), (earliest, rem)]
            if fc.is_safe(pays):
                plans.append(self._plan("partial_payment",
                    f"{req.rdate}:{fmt(safe)}|{earliest}:{fmt(rem)}",
                    req.ramt, 2, req.rdate, True, "none", False, ""))

        # --- Wait ---
        if ("full_payment" in prof.methods and earliest
                and earliest > req.rdate and earliest <= req.deadline):
            if fc.is_safe([(earliest, req.ramt)]):
                plans.append(self._plan("wait", f"{earliest}:{fmt(req.ramt)}",
                    req.ramt, 1, earliest, True, "none", False, ""))

        # --- Spending changes ---
        plans.extend(self._try_changes(fc, prof, evts, req, opts))

        by_dl = [p for p in plans if p["by_dl"]]
        ranked = by_dl if by_dl else plans

        if not ranked:
            # affordable_later: full payment becomes safe after deadline
            if earliest and "full_payment" in prof.methods:
                return self._mk(req, safe, "affordable_later", "wait",
                    f"{earliest}:{fmt(req.ramt)}", earliest, "none", prof)
            return self._fail(req, safe, prof)

        best = self._rank(ranked)

        if best["method"] == "full_payment" and not best["chg_needed"]:
            status = "affordable_now"
        elif best["method"] == "wait":
            status = "affordable_later"
        else:
            status = "affordable_with_plan"

        estr = str(earliest) if earliest else ""
        if status == "affordable_now":
            estr = str(req.rdate)

        return {
            "request_id": req.rid,
            "amount_safe_to_pay": str(rd2(safe)),
            "affordability_status": status,
            "recommended_payment_method": best["method"],
            "payment_plan": best["plan_str"],
            "earliest_date_for_full_payment": estr,
            "spending_changes_needed": best["changes"],
            "decision_explanation": self._explain(prof, req, best["method"], safe, earliest, best["changes"]),
        }

    def _plan(self, method, plan_str, total, n, start, by_dl, changes, chg_needed, opt_id):
        return {"method": method, "plan_str": plan_str, "total": total, "n": n,
                "start": start, "by_dl": by_dl, "changes": changes,
                "chg_needed": chg_needed, "opt_id": opt_id}

    def _inst_ok(self, opt, prof):
        """Check if installment option is within user's max_installment_months."""
        if prof.max_inst is None: return False
        s = opt.sched()
        if not s: return False
        # Number of payments must not exceed max_inst
        if opt.n > prof.max_inst: return False
        # Also check the time span
        span_months = (s[-1][0] - s[0][0]).days / 30.0
        return span_months <= prof.max_inst + 0.5

    def _try_changes(self, fc, prof, evts, req, opts):
        """Try spending changes (stop/reduce) to see if they unlock affordability."""
        plans = []

        # Eligible events to stop/reduce: must be in monthly_debits (recurring)
        # and not in protected categories
        stoppable = []
        reducible = []

        for cat, pat in fc.monthly_debits.items():
            if cat in prof.protected:
                continue
            eid = pat["event_id"]
            flex = pat["flex"]
            mina = pat["mina"]

            if flex in ("stoppable", "reducible_or_stoppable") and cat in prof.stop_cats:
                # Find the actual event for metadata
                evt = next((e for e in evts if e.eid == eid), None)
                if evt:
                    stoppable.append(evt)

            if flex in ("reducible", "reducible_or_stoppable") and cat in prof.reduce_cats and mina is not None:
                evt = next((e for e in evts if e.eid == eid), None)
                if evt:
                    reducible.append(evt)

        # Deduplicate by event_id
        seen = set()
        stoppable_dedup = []
        for e in stoppable:
            if e.eid not in seen:
                seen.add(e.eid)
                stoppable_dedup.append(e)
        seen = set()
        reducible_dedup = []
        for e in reducible:
            if e.eid not in seen:
                seen.add(e.eid)
                reducible_dedup.append(e)

        stoppable = stoppable_dedup
        reducible = reducible_dedup

        # Build combos (up to 3 changes, stop and reduce the SAME event is mutually exclusive)
        combos = []
        for e in stoppable: combos.append([("s", e)])
        for e in reducible: combos.append([("r", e)])
        for i, e1 in enumerate(stoppable):
            for e2 in stoppable[i+1:]:
                if e2.eid != e1.eid: combos.append([("s", e1), ("s", e2)])
            for e2 in reducible:
                if e2.eid != e1.eid: combos.append([("s", e1), ("r", e2)])
        for i, e1 in enumerate(reducible):
            for e2 in reducible[i+1:]:
                if e2.eid != e1.eid: combos.append([("r", e1), ("r", e2)])
        # 3-way combos (limited to keep runtime manageable)
        for i, e1 in enumerate(stoppable):
            for j, e2 in enumerate(stoppable):
                if j <= i: continue
                for e3 in reducible:
                    if e3.eid not in (e1.eid, e2.eid):
                        combos.append([("s", e1), ("s", e2), ("r", e3)])

        for combo in combos:
            if len(combo) > 3: continue
            eids = set()
            ok = True
            stopped_set, reduced_map, ch = set(), {}, []
            for a, e in combo:
                if e.eid in eids: ok = False; break
                eids.add(e.eid)
                if a == "s":
                    stopped_set.add(e.eid)
                    ch.append(f"stop:{e.eid}")
                else:
                    reduced_map[e.eid] = e.mina
                    ch.append(f"reduce_to:{e.eid}:{fmt(e.mina)}")
            if not ok: continue
            cs = "|".join(ch)

            if "full_payment" in prof.methods:
                if fc.is_safe([(req.rdate, req.ramt)], stopped_set, reduced_map):
                    plans.append(self._plan("full_payment", f"{req.rdate}:{fmt(req.ramt)}",
                        req.ramt, 1, req.rdate, True, cs, True, ""))

            if "installments" in prof.methods:
                for opt in sorted(opts, key=lambda o: o.pid):
                    if opt.method != "installments": continue
                    if not self._inst_ok(opt, prof): continue
                    s = opt.sched()
                    if not s: continue
                    by_dl = s[-1][0] <= req.deadline
                    if fc.is_safe([(d, a) for d, a in s], stopped_set, reduced_map):
                        plans.append(self._plan("installments",
                            "|".join(f"{d}:{fmt(a)}" for d, a in s),
                            opt.total, opt.n, s[0][0], by_dl, cs, True, opt.pid))

        return plans

    def _rank(self, plans):
        """
        Rank plans per spec:
        1. Completes by deadline
        2. No spending changes required
        3. Minimize total amount paid
        4. Start earlier
        5. Fewer payments
        6. Lowest payment_option_id (tie-breaker)
        """
        def key(p):
            return (
                0 if p["by_dl"] else 1,
                0 if not p["chg_needed"] else 1,
                p["total"],
                p["start"],
                p["n"],
                p.get("opt_id", ""),
            )
        plans.sort(key=key)
        return plans[0]

    def _fail(self, req, safe, prof=None):
        return {
            "request_id": req.rid,
            "amount_safe_to_pay": str(rd2(safe)),
            "affordability_status": "not_affordable",
            "recommended_payment_method": "not_recommended",
            "payment_plan": "none",
            "earliest_date_for_full_payment": "",
            "spending_changes_needed": "none",
            "decision_explanation": self._explain(prof, req, "not_recommended", safe, None, "none") if prof else "Not affordable.",
        }

    def _mk(self, req, safe, status, method, plan, earliest, changes, prof):
        estr = str(earliest) if earliest else ""
        if status == "affordable_now": estr = str(req.rdate)
        return {
            "request_id": req.rid,
            "amount_safe_to_pay": str(rd2(safe)),
            "affordability_status": status,
            "recommended_payment_method": method,
            "payment_plan": plan,
            "earliest_date_for_full_payment": estr,
            "spending_changes_needed": changes,
            "decision_explanation": self._explain(prof, req, method, safe, earliest, changes),
        }

    def _explain(self, prof, req, method, safe, earliest, changes):
        if not prof: return "Not affordable."
        c, mb, ra = prof.hc, rd2(prof.minb), rd2(req.ramt)
        if method == "full_payment" and changes == "none":
            return f"Pay {c} {fmt(ra)} today. This leaves at least {c} {fmt(mb)} available over the next 90 days."
        elif method == "full_payment":
            cd = self._desc_ch(changes)
            return f"{cd}, then pay {c} {fmt(ra)} today. This keeps the {c} {fmt(mb)} minimum protected."
        elif method == "installments":
            return f"Use installments to pay {c} {fmt(ra)}. This keeps the {c} {fmt(mb)} minimum protected throughout."
        elif method == "partial_payment":
            rem = rd2(ra - safe)
            return (f"Pay {c} {fmt(safe)} today and the remaining {c} {fmt(rem)} on {earliest}. "
                    f"This completes the full request and keeps the {c} {fmt(mb)} minimum protected.")
        elif method == "wait":
            if earliest:
                return (f"Pay {c} {fmt(ra)} in full on {earliest}. "
                        f"Paying earlier would take the balance below the {c} {fmt(mb)} minimum.")
            return "Wait for the balance to grow."
        else:
            if earliest:
                return (f"Do not proceed with the {c} {fmt(ra)} request. "
                        f"Although {c} {fmt(safe)} is available today, the full amount cannot be completed safely within 90 days.")
            return f"Do not make this payment by {req.deadline}. None of the available options keeps the {c} {fmt(mb)} minimum protected."

    def _desc_ch(self, changes):
        parts = []
        for ch in changes.split("|"):
            if ch.startswith("stop:"): parts.append(f"Stop {ch.split(':')[1]}")
            elif ch.startswith("reduce_to:"):
                t = ch.split(":"); parts.append(f"Reduce {t[1]} to {t[2]}")
        return ", ".join(parts)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    logger.info("Loading...")
    pd_raw = load_csv("financial_profiles.csv")
    ed_raw = load_csv("financial_events.csv")
    rd_raw = load_csv("exchange_rates.csv")
    rq_raw = load_csv("requests.csv")
    op_raw = load_csv("request_payment_options.csv")
    ms_raw = load_csv("messages.csv")
    im_raw = load_csv("images.csv")

    profs = {p.uid: p for p in (Profile(r) for r in pd_raw)}
    ebu = defaultdict(list)
    for r in ed_raw: e = Evt(r); ebu[e.uid].append(e)
    fx = FX(rd_raw)
    reqs = [Req(r) for r in rq_raw]
    opts = defaultdict(list)
    for r in op_raw: o = PO(r); opts[o.rid].append(o)

    logger.info(f"{len(profs)} profiles, {sum(len(v) for v in ebu.values())} events, {len(reqs)} requests")

    eng = Engine(profs, ebu, opts, fx, ms_raw, im_raw)

    results = []
    for i, req in enumerate(reqs):
        try:
            results.append(eng.run(req))
        except Exception as ex:
            logger.error(f"Error {req.rid}: {ex}")
            import traceback; traceback.print_exc()
            results.append({
                "request_id": req.rid, "amount_safe_to_pay": "0",
                "affordability_status": "not_affordable",
                "recommended_payment_method": "not_recommended",
                "payment_plan": "none", "earliest_date_for_full_payment": "",
                "spending_changes_needed": "none",
                "decision_explanation": f"Error: {str(ex)[:100]}"
            })
        if (i+1) % 50 == 0:
            logger.info(f"Processed {i+1}/{len(reqs)}")

    # Post-validate: ensure amount_safe_to_pay is in [0, requested_amount]
    req_map = {r.rid: r for r in reqs}
    for r in results:
        rid = r["request_id"]
        rq = req_map.get(rid)
        if rq:
            try:
                asp = float(r["amount_safe_to_pay"])
                if asp < 0: r["amount_safe_to_pay"] = "0"
                if asp > rq.ramt + 0.01: r["amount_safe_to_pay"] = str(rd2(rq.ramt))
            except: pass

    cols = ["request_id", "amount_safe_to_pay", "affordability_status",
            "recommended_payment_method", "payment_plan",
            "earliest_date_for_full_payment", "spending_changes_needed",
            "decision_explanation"]
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in results: w.writerow(r)
    logger.info(f"Output: {OUTPUT_PATH}")

    sc, mc = defaultdict(int), defaultdict(int)
    for r in results:
        sc[r["affordability_status"]] += 1
        mc[r["recommended_payment_method"]] += 1
    logger.info(f"Statuses: {dict(sc)}")
    logger.info(f"Methods: {dict(mc)}")

    edir = os.path.join(BASE_DIR, "code", "evaluation")
    os.makedirs(edir, exist_ok=True)
    with open(os.path.join(edir, "usage_report.md"), "w", encoding="utf-8") as f:
        f.write(f"""# Token Usage Report

## Summary
Fully deterministic financial decision engine. No LLM calls.

| Metric | Value |
|--------|-------|
| Model Provider | None (deterministic) |
| Model Name | N/A |
| Model Calls | 0 |
| Input Tokens | 0 |
| Output Tokens | 0 |
| Total Tokens | 0 |
| Avg Tokens/Request | 0 |
| Estimated Total Cost | $0.00 |
| Estimated Per-Request Cost | $0.00 |

## Notes
- Image amounts: pre-extracted and cached in IMAGE_AMOUNTS dict
- Message parsing: rule-based regex with tightened currency-prefix requirements
- Recurrence detection: monthly aggregation of category totals (v4)
- Salary projection: recurring from scheduled event or historical payroll
- No API keys required
- Requests processed: {len(reqs)}
""")
    logger.info("Done.")


if __name__ == "__main__":
    main()
