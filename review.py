#!/usr/bin/env python3
"""Split an enriched listing tab into maintenance buckets for review.

From an enriched source tab (produced by refresh.py), subtract listings already
chosen (--preferiti) or rejected (--ignore-tab), drop the hard exclusions (bare
ownership, auctions), then split the rest by maintenance level into three tabs:

  <prefix>-pronti          ready to use (Manutenzione = nessuna)
  <prefix>-ordinaria       light works
  <prefix>-straordinaria   heavy works

Costs differ by bucket (ready = price + agency; works add on top), so each tab
is sorted by all-in cost per m2 and the good-deal mark (green Costo tot./m2) is
the cheapest quartile within that bucket. Outdoor space stays tinted green.

Usage:
    python3 review.py --spreadsheet <ID|URL> --source Appartamenti-ricerca \
        --prefix Appartamenti --account you@example.com
"""

import argparse
import json
import statistics
import subprocess
import urllib.request

PM2_FORMULA = '=INDIRECT("C"&ROW())/INDIRECT("D"&ROW())'
BUCKETS = ("pronti", "ordinaria", "straordinaria")
# review-decision fields tracked in the bucket tabs and preserved across runs
STICKY = ("Stato", "Note", "Adatto affitto")
STATO_OPTIONS = ["da vedere", "da contattare", "visita fissata", "visitato",
                 "interessante", "scartare"]


def token(account):
    args = ["gcloud", "auth", "print-access-token"] + ([account] if account else [])
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("gcloud token failed; run: gcloud auth login "
                         "--enable-gdrive-access\n" + out.stderr.strip())
    return out.stdout.strip()


def sheets(tok, sid, path, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}{path}",
        data=data, method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))


def parse_spreadsheet_id(s):
    if "/d/" in s:
        return s.split("/d/", 1)[1].split("/")[0].split("?")[0].split("#")[0]
    return s


def tab_ids(tok, sid):
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in sheets(tok, sid, "?fields=sheets.properties")["sheets"]}


def ensure_tab(tok, sid, title, ids):
    if title in ids:
        return ids[title]
    rep = sheets(tok, sid, ":batchUpdate",
                 {"requests": [{"addSheet": {"properties": {"title": title}}}]}, "POST")
    return rep["replies"][0]["addSheet"]["properties"]["sheetId"]


def sticky_map(tok, sid, tab):
    """Read review-decision fields (STICKY) keyed by URL from an existing tab."""
    try:
        rows = sheets(tok, sid, f"/values/{tab}!A1:AZ4000"
                      "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    except urllib.error.HTTPError:
        return {}
    if not rows:
        return {}
    hidx = {h: i for i, h in enumerate(rows[0])}
    cols = {f: hidx[f] for f in STICKY if f in hidx}
    out = {}
    for r in rows[1:]:
        if not r or "immobiliare.it/annunci" not in str(r[0]):
            continue
        vals = {f: (str(r[i]) if i < len(r) and r[i] not in (None, "") else "")
                for f, i in cols.items()}
        if any(vals.values()):
            out[str(r[0]).rstrip("/")] = vals
    return out


def bucket_of(man):
    m = str(man).strip().lower()
    if m == "nessuna":
        return "pronti"
    if m.startswith("straordinaria"):
        return "straordinaria"
    return "ordinaria"  # ordinaria, ordinaria?, unknown -> light-works bucket


def write_bucket(tok, sid, tab, ids, H, rows, idx, PM2, EST, TM):
    sh = ensure_tab(tok, sid, tab, ids)
    sheets(tok, sid, f"/values/{tab}!A1:AZ4000:clear", {}, "POST")
    sheets(tok, sid, f"/values/{tab}!A1?valueInputOption=USER_ENTERED",
           {"values": [H] + rows}, "PUT")
    n, NC = len(rows) + 1, len(H)

    def c(r, g, b):
        return {"red": r / 255, "green": g / 255, "blue": b / 255}
    euro = [idx["Prezzo (EUR)"], idx["Costo lavori (EUR)"], idx["Costo totale (EUR)"],
            idx["Costo tot./m2"], PM2]
    meta = sheets(tok, sid, "?fields=sheets(properties(sheetId),bandedRanges(bandedRangeId))")
    bands = [b["bandedRangeId"] for s in meta["sheets"]
             if s["properties"]["sheetId"] == sh for b in s.get("bandedRanges", [])]
    reqs = [{"deleteBanding": {"bandedRangeId": b}} for b in bands]
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sh,
            "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": n,
            "startColumnIndex": 0, "endColumnIndex": NC}, "cell": {"userEnteredFormat":
            {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": NC}, "cell": {"userEnteredFormat":
            {"backgroundColor": c(31, 41, 55), "textFormat": {"bold": True,
             "foregroundColor": c(255, 255, 255)}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
    ]
    if n > 1:
        reqs.append({"addBanding": {"bandedRange": {"range": {"sheetId": sh,
            "startRowIndex": 1, "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": NC},
            "rowProperties": {"firstBandColor": c(255, 255, 255),
             "secondBandColor": c(238, 242, 247)}}}})
    reqs.append({"setBasicFilter": {"filter": {"range": {"sheetId": sh, "startRowIndex": 0,
        "startColumnIndex": 0, "endColumnIndex": NC}}}})
    if "Stato" in idx and n > 1:  # decision dropdown on the Stato column
        reqs.append({"setDataValidation": {"range": {"sheetId": sh, "startRowIndex": 1,
            "endRowIndex": n, "startColumnIndex": idx["Stato"], "endColumnIndex": idx["Stato"] + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": o} for o in STATO_OPTIONS]},
                "showCustomUi": True, "strict": False}}})
    for ci in euro:
        reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 1,
            "startColumnIndex": ci, "endColumnIndex": ci + 1}, "cell": {"userEnteredFormat":
            {"numberFormat": {"type": "CURRENCY", "pattern": "€ #,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}})

    def totm2(r):
        try:
            return float(r[TM])
        except (ValueError, TypeError):
            return float("inf")
    tm_vals = sorted(v for v in (totm2(r) for r in rows) if v != float("inf"))
    cutoff = tm_vals[int(0.25 * len(tm_vals))] if tm_vals else 0
    deals = 0
    for i, r in enumerate(rows, 2):
        if str(r[EST]).strip().lower() not in ("", "no"):
            reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": i - 1,
                "endRowIndex": i, "startColumnIndex": EST, "endColumnIndex": EST + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": c(212, 237, 218)}},
                "fields": "userEnteredFormat.backgroundColor"}})
        if totm2(r) <= cutoff:
            deals += 1
            reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": i - 1,
                "endRowIndex": i, "startColumnIndex": TM, "endColumnIndex": TM + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": c(76, 175, 80),
                    "textFormat": {"bold": True, "foregroundColor": c(255, 255, 255)}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
    sheets(tok, sid, ":batchUpdate", {"requests": reqs}, "POST")
    return len(rows), deals


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spreadsheet", required=True)
    ap.add_argument("--source", default="Appartamenti-ricerca")
    ap.add_argument("--prefix", default="Appartamenti")
    ap.add_argument("--preferiti", default="Appartamenti-preferiti")
    ap.add_argument("--ignore-tab", default="Ignorati")
    ap.add_argument("--retire", default="Appartamenti-da-valutare",
                    help="old combined tab to delete once split (blank to keep)")
    ap.add_argument("--account", default="")
    args = ap.parse_args()

    sid = parse_spreadsheet_id(args.spreadsheet)
    tok = token(args.account)

    def urls_in(tab):
        try:
            rows = sheets(tok, sid, f"/values/{tab}!A2:A4000").get("values", [])
        except urllib.error.HTTPError:
            return set()
        return {r[0].rstrip("/") for r in rows if r and "immobiliare.it/annunci" in str(r[0])}

    pref_urls = urls_in(args.preferiti)
    ignore_urls = urls_in(args.ignore_tab)

    # preserve review decisions made in the bucket tabs across regeneration
    preserved = {}
    for b in BUCKETS:
        preserved.update(sticky_map(tok, sid, f"{args.prefix}-{b}"))

    # listings marked "scartare" move to the ignore tab and drop out of review
    scartare = {u for u, v in preserved.items()
                if str(v.get("Stato", "")).strip().lower() == "scartare"}
    new_rejects = sorted(scartare - ignore_urls)
    if new_rejects:
        sheets(tok, sid, f"/values/{args.ignore_tab}!A1:append?valueInputOption=USER_ENTERED",
               {"values": [[u + "/", "scartato in review"] for u in new_rejects]}, "POST")
    subtract = pref_urls | ignore_urls | scartare

    v = sheets(tok, sid, f"/values/{args.source}!A1:AZ2000"
               "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    if not v:
        raise SystemExit(f"source tab {args.source} is empty")
    H = v[0]
    idx = {h: i for i, h in enumerate(H)}
    PROP, MAN, EST, TIT = idx["Proprietà"], idx["Manutenzione"], idx["Esterni"], idx["Titolo"]
    TM, PM2 = idx["Costo tot./m2"], len(H) - 1
    sticky_idx = {f: idx[f] for f in STICKY if f in idx}

    def val(r, i):
        return r[i] if i < len(r) else ""

    parts = {b: [] for b in BUCKETS}
    dropped = {"nuda": 0, "auction": 0, "preferiti/ignorati": 0}
    for r in v[1:]:
        r = list(r) + [""] * (len(H) - len(r))
        if str(r[0]).rstrip("/") in subtract:
            dropped["preferiti/ignorati"] += 1
            continue
        if str(val(r, PROP)).strip().lower() == "nuda":
            dropped["nuda"] += 1
            continue
        if "asta" in str(val(r, TIT)).lower():
            dropped["auction"] += 1
            continue
        r[PM2] = PM2_FORMULA
        # re-apply review decisions kept from the previous bucket tabs
        prev = preserved.get(str(r[0]).rstrip("/"), {})
        for f, i in sticky_idx.items():
            if prev.get(f):
                r[i] = prev[f]
        parts[bucket_of(val(r, MAN))].append(r)

    def totm2(r):
        try:
            return float(r[TM])
        except (ValueError, TypeError):
            return float("inf")

    ids = tab_ids(tok, sid)
    summary = []
    for b in BUCKETS:
        rows = sorted(parts[b], key=totm2)
        kept, deals = write_bucket(tok, sid, f"{args.prefix}-{b}", ids, H, rows, idx, PM2, EST, TM)
        summary.append(f"{b}={kept} ({deals} deals)")

    if args.retire:
        ids = tab_ids(tok, sid)
        if args.retire in ids:
            sheets(tok, sid, ":batchUpdate",
                   {"requests": [{"deleteSheet": {"sheetId": ids[args.retire]}}]}, "POST")
    moved = f", moved {len(new_rejects)} scartare -> {args.ignore_tab}" if new_rejects else ""
    print(f"{args.source} -> " + ", ".join(summary) + f"; dropped {dropped}{moved}")


if __name__ == "__main__":
    main()
