#!/usr/bin/env python3
"""Build a catalogue of WDC meetings, documents and agenda-item topics.

    python3 build_catalog.py REPO_DIR [--sources DIR] [--out DIR] [--text-cache DIR]

Inputs
  * eScribe listings in --sources (default: catalog/sources next to this script):
      *.psv          meeting|date|time|venue|kind|doc_id|label   (kind: Agenda, Addendum,
                     Minutes, Cover, Attachment; '#' lines are comments)
      *.txt          listings pasted from the eScribe portal, with [PDF](...DocumentId=N) links
      *.nolinks.txt  listings copied as plain text (no links), e.g. "AgendaPDF", "MinutesPDF"
  * the eScribe entries already in REPO_DIR/index.json and minutes.json
  * the PDFs in REPO_DIR (agendas, addenda and minutes are read for agenda items)

Outputs (in --out, default: catalog/ next to this script)
  meetings.json   one record per meeting: documents, agenda items, topics
  documents.json  flat list in the same shape as index.json, plus eScribe ids
  topics.json     topic -> agenda items across all meetings
  missing-urls.txt  eScribe URLs for documents not yet in the repo
  COVERAGE.md     what is and isn't mirrored, by meeting type and year
Needs pdftotext (poppler-utils) for agenda-item extraction.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import OrderedDict, defaultdict

ESCRIBE = "https://pub-wdc.escribemeetings.com/FileStream.ashx?DocumentId="
PAGES = "https://eileenmcnaughton.github.io/wdc-minutes/"
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
MON3 = {k[:3]: v for k, v in MONTHS.items()}
MON3["sept"] = 9

# ---------------------------------------------------------------- topics
# Tags applied to agenda item titles (case-insensitive regex). Extend freely.
TOPICS = OrderedDict([
    ("Transport: T2 / bus priority", r"\bT2\b|transit lane|priority lane|special vehicle lane|bus (?:priority|lane)"),
    ("Transport: public transport", r"\bbus(es)?\b|public transport|rose st(reet)? bus|citylink|passenger transport|Transport Choices"),
    ("Transport: roads & traffic", r"\broad(s|ing)?\b|roundabout|intersection|traffic|speed limit|bridge|footpath|seal|NZTA|Waka Kotahi|state highway|\bSH1\b|Springs Flat"),
    ("Transport: walking & cycling", r"shared path|cycl|walking|cycleway|Transport Choices"),
    ("Parking", r"parking|car ?park"),
    ("Water supply", r"\bwater (supply|treatment|main|take)|drinking water|\bdam\b|reservoir|Water Done Well|LWDW|NWDW|Northland Waters"),
    ("Wastewater", r"wastewater|sewer|WWTP|RWWTP"),
    ("Stormwater & flooding", r"stormwater|flood"),
    ("Storms & emergencies", r"storm|emergency|cyclone|civil defence|CDEM|recovery"),
    ("Finance: annual plan / LTP / rates", r"annual plan|long term plan|\bLTP\b|\bAP2?\d|rates?\b|rating|financial (strategy|parameters|report)|budget|treasury|debt"),
    ("Finance: annual report & audit", r"annual report|audit|Deloitte|Audit NZ|risk"),
    ("Contracts & procurement", r"contract|tender|procurement|CON\d{4,}|variation|delegated authority"),
    ("Bylaws & regulation", r"bylaw|dog|animal|alcohol|freedom camping|signs|public places|trading|vehicles on beaches"),
    ("District plan & RMA", r"district plan|plan change|\bPC\d|resource management|\bRMA\b|section 32|designation|spatial plan|future development strategy|\bFDS\b|growth strateg"),
    ("Property & facilities", r"property|lease|licence|civic centre|Te Iwitahi|library|pool|stadium|events centre|boat shed|pensioner housing|reserve"),
    ("Community & events", r"community fund|grant|event|creative communities|youth|accessib|welcoming communities|civic honours|arts"),
    ("Māori partnership", r"Te K[aā]rearea|hap[uū]|marae|M[aā]ori|iwi|Te Tiriti|kaupapa"),
    ("Council governance", r"minutes|public forum|delegation|standing orders|elected member|elections?|representation|chief executive|remuneration|CCO|CCTO|holdings|Northland Inc|local government reform|simplification"),
    ("Waste & environment", r"waste|recycl|landfill|climate|coastal|biodiversity|environment"),
    ("Economic development & city centre", r"economic|city centre|CBD|placemaking|waterfront|town basin|Pohe Island"),
])
TOPIC_RES = [(t, re.compile(p, re.I)) for t, p in TOPICS.items()]
# Document-level full-text flags: count of mentions in the extracted text
FLAGS = OrderedDict([("T2 / transit lane", r"\bT2\b|transit lane|priority lane|special vehicle lane")])
FLAG_RES = [(t, re.compile(p, re.I)) for t, p in FLAGS.items()]


def norm(s):
    """Normalise a meeting name for matching (strip macrons, case, punctuation)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def iso(d, mon, y):
    m = MONTHS.get(mon.lower()) or MON3.get(mon.lower()[:4]) or MON3.get(mon.lower()[:3])
    return dt.date(int(y), m, int(d)).isoformat()


def to24(t):
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", t.strip(), re.I)
    if not m:
        return t.strip()
    h, mi, ap = int(m.group(1)), m.group(2), m.group(3).upper()
    h = h % 12 + (12 if ap == "PM" else 0)
    return f"{h:02d}:{mi}"


def kind_of(word):
    w = word.strip().lower()
    if w.startswith("agenda cover") or w.startswith("agenda table of contents"):
        return "Cover"
    for k in ("Agenda", "Addendum", "Minutes"):
        if w.startswith(k.lower()) or (k == "Minutes" and "minutes" in w):
            return k
    if w.startswith("attach"):
        return "Attachment"
    return None


# ---------------------------------------------------------------- source parsers
DATE_LINE = re.compile(r"^\w+day, (\d{1,2}) (\w+), (\d{4}) @ (.+)$")
LINK = re.compile(r"\[([^\]]+)\]\((https://pub-wdc\.escribemeetings\.com/FileStream\.ashx\?DocumentId=(\d+))\)")


def parse_psv(path):
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        meeting, date, time, venue, kind, docid, label = (line.split("|") + [""] * 7)[:7]
        yield dict(meeting=meeting, date=date, time=time, venue=venue, kind=kind,
                   doc_id=docid or None, label=label, source=os.path.basename(path))


def _meeting_headers(lines):
    """Yield (index_of_date_line, meeting, date, time, venue)."""
    for i, line in enumerate(lines):
        m = DATE_LINE.match(line)
        if not m:
            continue
        j = i - 1
        while j >= 0 and not lines[j].strip("* ").strip():
            j -= 1
        venue = lines[i + 1].strip() if i + 1 < len(lines) else ""
        yield i, lines[j].strip(), iso(m.group(1), m.group(2), m.group(3)), to24(m.group(4)), venue


def parse_paste(path):
    lines = [l.strip() for l in open(path, encoding="utf-8").read().splitlines()]
    heads = list(_meeting_headers(lines))
    for n, (i, meeting, date, time, venue) in enumerate(heads):
        end = heads[n + 1][0] - 2 if n + 1 < len(heads) else len(lines)
        pending = None
        for line in lines[i + 2:end]:
            bare = line.strip("* ").strip()
            k = kind_of(bare) if bare and "[" not in bare else None
            if k and k != "Attachment":
                pending = k
                continue
            for label, url, docid in LINK.findall(line):
                if label == "PDF" and pending:
                    yield dict(meeting=meeting, date=date, time=time, venue=venue, kind=pending,
                               doc_id=docid, label="", source=os.path.basename(path))
                else:
                    yield dict(meeting=meeting, date=date, time=time, venue=venue, kind="Attachment",
                               doc_id=docid, label=label, source=os.path.basename(path))


def parse_nolinks(path):
    lines = [l.strip() for l in open(path, encoding="utf-8").read().splitlines()]
    heads = list(_meeting_headers(lines))
    for n, (i, meeting, date, time, venue) in enumerate(heads):
        end = heads[n + 1][0] - 1 if n + 1 < len(heads) else len(lines)
        for line in lines[i + 2:end]:
            if not line or line == "See More":
                continue
            m = re.match(r"^(Agenda Cover Page|Agenda|Addendum|Minutes)(PDF)+$", line)
            if m:
                yield dict(meeting=meeting, date=date, time=time, venue=venue, kind=kind_of(m.group(1)),
                           doc_id=None, label="", source=os.path.basename(path))
            else:
                yield dict(meeting=meeting, date=date, time=time, venue=venue, kind="Attachment",
                           doc_id=None, label=line, source=os.path.basename(path))


def parse_existing_index(repo):
    """eScribe-only entries the repo's own index.json / minutes.json already hold."""
    for fn in ("index.json", "minutes.json"):
        p = os.path.join(repo, fn)
        if not os.path.exists(p):
            continue
        for e in json.load(open(p)):
            raw = e.get("url")
            urls = (raw if isinstance(raw, list) else [raw]) + [u.get("url") if isinstance(u, dict) else u
                                                                for u in e.get("urls") or []]
            for u in urls:
                if not isinstance(u, str):
                    continue
                m = re.search(r"escribemeetings\.com/FileStream\.ashx\?DocumentId=(\d+)", u or "")
                if m and e.get("date"):
                    cat = e.get("category") or e.get("type") or ""
                    name = (e.get("meeting") or e.get("title") or "").strip()
                    if name != "Information Agenda":
                        name = re.sub(r"\s+(Agenda|Addendum|Minutes|Agenda Addendum)$", "", name)
                    yield dict(meeting=name, date=e["date"], time="", venue="",
                               kind=kind_of(cat) or "Attachment", doc_id=m.group(1),
                               label=e.get("filename", "") if kind_of(cat) == "Attachment" else "",
                               source=fn)


# ---------------------------------------------------------------- repo scan
FILE_RE = re.compile(r"^(?P<cat>Agenda Package|Agenda Table of Contents|Agenda Cover Page|Addendum Package|Addendum|"
                     r"Post-Meeting Minutes|Minutes) - (?P<meeting>.+?)_(?P<d>\d{1,2})(?P<mon>[A-Za-z]{3,4})_(?P<y>\d{4})")


def scan_repo(repo):
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.lower().endswith((".pdf", ".pptx", ".docx")):
                continue
            rel = os.path.relpath(os.path.join(root, f), repo).replace(os.sep, "/")
            m = FILE_RE.match(f)
            rec = dict(path=rel, filename=f)
            if m:
                rec.update(kind=kind_of(m.group("cat")), meeting=m.group("meeting").replace("_", " ").strip(),
                           date=iso(m.group("d"), m.group("mon"), m.group("y")))
            out.append(rec)
    return out


# ---------------------------------------------------------------- agenda items
ITEM_RE = re.compile(r"^\s{0,8}(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s{1,6}(\S.*?)(?:\s{2,}(\d{1,4}))?\s*$")


def pdf_text(path, cache_dir, first=1, last=None):
    key = re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(path)) + f".{first}-{last}.txt"
    cp = os.path.join(cache_dir, key)
    if os.path.exists(cp):
        return open(cp, encoding="utf-8", errors="replace").read()
    args = ["pdftotext", "-layout", "-f", str(first)] + (["-l", str(last)] if last else []) + [path, "-"]
    try:
        txt = subprocess.run(args, capture_output=True, timeout=180).stdout.decode("utf-8", "replace")
    except Exception:
        txt = ""
    with open(cp, "w", encoding="utf-8") as fh:
        fh.write(txt)
    return txt


def agenda_items(text):
    """Pull numbered items from the table of contents at the front of an agenda/minutes."""
    lines = text.splitlines()
    items, last = [], None
    for idx, line in enumerate(lines[:400]):
        m = ITEM_RE.match(line)
        if m and not re.search(r"\.{5,}", m.group(2)):
            num, title, page = m.group(1), m.group(2).strip(), m.group(3)
            title = re.split(r"\s+Moved By\b|\s{3,}", title)[0].strip()
            if len(title) < 3 or title.lower().startswith(("that ", "moved", "seconded")):
                continue
            if items and items[-1]["number"] == num:
                continue
            last = dict(number=num, title=title, page=int(page) if page else None)
            items.append(last)
            continue
        # continuation of a wrapped title: indented text, no number, right after an item
        if last and line.strip() and re.match(r"^\s{6,}\S", line) and not re.search(r"\d{1,4}\s*$", line) is None:
            pass
        if last and line.strip() and re.match(r"^\s{6,}[A-Za-zĀ-ū(–-]", line) and len(line.strip()) < 70 \
                and not ITEM_RE.match(line) and idx > 0 and lines[idx - 1].strip():
            cont = re.sub(r"\s{2,}\d{1,4}\s*$", "", line.strip())
            if last["page"] is None:
                pm = re.search(r"\s{2,}(\d{1,4})\s*$", line)
                if pm:
                    last["page"] = int(pm.group(1))
            if not cont.lower().startswith(("page", "item")):
                last["title"] = (last["title"] + " " + cont).strip()
            last = None if last["page"] else last
        elif not line.strip():
            last = None
    # keep the first run of items (the table of contents), stop when numbering restarts at the body
    seen, toc = set(), []
    for it in items:
        if it["number"] in seen:
            break
        seen.add(it["number"])
        it["title"] = re.split(r"\s+Moved By\b|\s+Recommendation(?:s)?:?\s*$", it["title"])[0].strip()
        toc.append(it)
    return toc


def tag(title):
    return [t for t, rx in TOPIC_RES if rx.search(title)]


# ---------------------------------------------------------------- build
def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--sources", default=os.path.join(here, "sources"))
    ap.add_argument("--out", default=here)
    ap.add_argument("--text-cache", default=os.path.join(here, ".textcache"))
    ap.add_argument("--no-text", action="store_true", help="skip PDF text extraction")
    ap.add_argument("--read-legacy-index", action="store_true",
                    help="also read eScribe links from a hand-built REPO/index.json (only needed once; now in sources/legacy-index.psv)")
    ap.add_argument("--write-repo-index", action="store_true",
                    help="also write REPO/index.json, REPO/minutes.json, REPO/data.json and per-year meeting files")
    ap.add_argument("--raw-base", default="https://raw.githubusercontent.com/eileenmcnaughton/wdc-minutes/refs/heads/master/",
                    help="base URL used for the file links in data.json")
    a = ap.parse_args()
    os.makedirs(a.text_cache, exist_ok=True)

    # 1. eScribe documents from all sources
    rows = []
    for f in sorted(os.listdir(a.sources)):
        p = os.path.join(a.sources, f)
        if f.endswith(".psv"):
            rows += parse_psv(p)
        elif f.endswith(".nolinks.txt"):
            rows += parse_nolinks(p)
        elif f.endswith(".txt"):
            rows += parse_paste(p)
    if a.read_legacy_index:
        rows += parse_existing_index(a.repo)

    meetings = OrderedDict()  # (norm meeting, date, time) -> meeting dict

    def meeting_for(name, date, time, venue):
        key = (norm(name), date)
        cands = [k for k in meetings if k[:2] == key]
        if time:
            exact = [k for k in cands if k[2] == time]
            if exact:
                k = exact[0]
            elif len(cands) == 1 and not meetings[cands[0]]["time"]:
                k = cands[0]
                meetings[(k[0], k[1], time)] = meetings.pop(k)
                k = (k[0], k[1], time)
                meetings[k]["time"] = time
            else:
                k = (key[0], date, time)
        else:
            k = cands[0] if cands else (key[0], date, "")
        if k not in meetings:
            meetings[k] = dict(meeting=name, date=date, time=time, venue=venue, documents=[], items=[], topics=[])
        mt = meetings[k]
        if venue and not mt["venue"]:
            mt["venue"] = venue
        if "ā" in name and "ā" not in mt["meeting"]:
            mt["meeting"] = name
        return mt

    by_docid = {}
    linkless_seen = {}
    for r in rows:
        if r["doc_id"] and r["doc_id"] in by_docid:
            continue
        mt = meeting_for(r["meeting"], r["date"], r["time"], r["venue"])
        docs = mt["documents"]
        if r["doc_id"]:
            # replace a link-less placeholder of the same kind/label from a .nolinks listing
            for d in docs:
                if not d["escribe_doc_id"] and d["kind"] == r["kind"] and (
                        r["kind"] != "Attachment" or not r["label"] or d["label"][:12] == r["label"][:12]):
                    d.update(escribe_doc_id=r["doc_id"], escribe_url=ESCRIBE + r["doc_id"])
                    if r["label"] and (not d["label"] or ("...." in d["label"] and "...." not in r["label"])):
                        d["label"] = r["label"]
                    by_docid[r["doc_id"]] = d
                    break
            else:
                d = dict(kind=r["kind"], label=r["label"], escribe_doc_id=r["doc_id"],
                         escribe_url=ESCRIBE + r["doc_id"], path=None)
                docs.append(d)
                by_docid[r["doc_id"]] = d
        else:
            if any(d["kind"] == r["kind"] and d["label"] == r["label"] and r["kind"] != "Attachment" for d in docs):
                continue
            if r["kind"] == "Attachment":
                blank = next((d for d in docs if d["kind"] == "Attachment" and d["escribe_doc_id"] and not d["label"]), None)
                if blank:
                    blank["label"] = r["label"]
                    continue
                # same attachment already listed with a link (names truncated slightly differently)
                pre = r["label"][:12].lower()
                same = [d for d in docs if d["kind"] == "Attachment" and d["label"][:12].lower() == pre]
                seen_key = (id(mt), pre)
                claimed = linkless_seen.get(seen_key, 0)
                if claimed < sum(1 for d in same if d["escribe_doc_id"]):
                    linkless_seen[seen_key] = claimed + 1
                    continue
            docs.append(dict(kind=r["kind"], label=r["label"], escribe_doc_id=None, escribe_url=None, path=None))

    # 2. match repo files
    repo_files = scan_repo(a.repo)
    loose = []
    for f in repo_files:
        if "kind" not in f:
            loose.append(f)
            continue
        cands = [m for k, m in meetings.items() if k[0] == norm(f["meeting"]) and k[1] == f["date"]]
        mt = cands[0] if cands else meeting_for(f["meeting"], f["date"], "", "")
        target = next((d for d in mt["documents"] if d["kind"] == f["kind"] and not d["path"]), None)
        if target is None:
            target = dict(kind=f["kind"], label="", escribe_doc_id=None, escribe_url=None, path=None)
            mt["documents"].append(target)
        target["path"] = f["path"]
    # attachments: eScribe truncates names ("Item 4.1 Attachment 1 W....pdf") but keeps the prefix
    for f in loose:
        stem = f["filename"].lower()
        fdate = None
        dm = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|"
                       r"October|November|December)\s+(20\d\d)\b", f["filename"], re.I)
        if dm:
            fdate = iso(dm.group(1), dm.group(2), dm.group(3))
        cands = []
        for mt in meetings.values():
            for d in mt["documents"]:
                if d["kind"] == "Attachment" and not d["path"] and d["label"]:
                    pre = re.split(r"\.{3,}|\.(pdf|pptx|docx)$", d["label"])[0].strip().lower()
                    if len(pre) >= 12 and stem.startswith(pre):
                        cands.append((len(pre), mt, d))
        if fdate and any(c[1]["date"] == fdate for c in cands):  # a date in the file name settles it first
            cands = [c for c in cands if c[1]["date"] == fdate]
        best = max((c[0] for c in cands), default=0)
        hits = [c[2] for c in cands if c[0] == best]
        if len(hits) == 1:  # longest matching prefix wins; ties stay unlinked
            hits[0]["path"] = f["path"]
            f["matched"] = True

    # 3. agenda items + topics from repo text
    if not a.no_text:
        for mt in meetings.values():
            docs = {d["kind"]: d for d in mt["documents"] if d["path"] and d["path"].lower().endswith(".pdf")}
            src = docs.get("Agenda") or docs.get("Minutes")
            items = []
            if src:
                items = agenda_items(pdf_text(os.path.join(a.repo, src["path"]), a.text_cache, 1, 6))
                for it in items:
                    it["source"] = src["kind"]
            if "Addendum" in docs:
                for it in agenda_items(pdf_text(os.path.join(a.repo, docs["Addendum"]["path"]), a.text_cache, 1, 4)):
                    if not any(x["number"] == it["number"] for x in items):
                        it["source"] = "Addendum"
                        items.append(it)
            for it in items:
                it["topics"] = tag(it["title"])
            mt["items"] = items
            # full-text flags on every mirrored PDF
            for d in mt["documents"]:
                if d["path"] and d["path"].lower().endswith(".pdf"):
                    txt = pdf_text(os.path.join(a.repo, d["path"]), a.text_cache)
                    fl = {t: len(rx.findall(txt)) for t, rx in FLAG_RES}
                    fl = {t: n for t, n in fl.items() if n}
                    if fl:
                        d["mentions"] = fl
    for mt in meetings.values():
        tops = OrderedDict()
        for it in mt["items"]:
            for t in it.get("topics", []):
                tops[t] = 1
        for d in mt["documents"]:
            if d.get("label"):
                for t in tag(d["label"]):
                    tops[t] = 1
        mt["topics"] = list(tops)
        for d in mt["documents"]:
            d["in_repo"] = bool(d["path"])
            d["url"] = (PAGES + "/".join(map(lambda s: __import__("urllib.parse").parse.quote(s), d["path"].split("/")))) \
                if d["path"] else d["escribe_url"]

    ordered = sorted(meetings.values(), key=lambda m: (m["date"], m["time"] or "99", m["meeting"]), reverse=True)
    for i, mt in enumerate(ordered):
        mt["id"] = f"{mt['date']}_{re.sub(r'[^a-z0-9]+', '-', norm(mt['meeting']))}" + (
            f"_{mt['time'].replace(':', '')}" if sum(1 for x in ordered if x['date'] == mt['date'] and norm(x['meeting']) == norm(mt['meeting'])) > 1 else "")
        order = {"Cover": 0, "Agenda": 1, "Addendum": 2, "Attachment": 3, "Minutes": 4}
        mt["documents"].sort(key=lambda d: (order.get(d["kind"], 9), d["escribe_doc_id"] or "", d["label"]))

    # 4. write outputs
    os.makedirs(a.out, exist_ok=True)
    meetings_out = [OrderedDict([("id", m["id"]), ("meeting", m["meeting"]), ("date", m["date"]), ("time", m["time"]),
                                 ("venue", m["venue"]), ("topics", m["topics"]), ("items", m["items"]),
                                 ("documents", m["documents"])]) for m in ordered]
    json.dump(meetings_out, open(os.path.join(a.out, "meetings.json"), "w"), ensure_ascii=False, separators=(",", ":"))

    docs_out = []
    for m in ordered:
        for d in m["documents"]:
            docs_out.append(OrderedDict([
                ("date", m["date"]), ("meeting", m["meeting"]), ("category", d["kind"]),
                ("filename", os.path.basename(d["path"]) if d["path"] else (d["label"] or None)),
                ("path", d["path"]), ("url", d["url"]),
                ("repo_url", d["url"] if d["in_repo"] else None),
                ("escribe_doc_id", d["escribe_doc_id"]),
                ("escribe_url", d["escribe_url"]), ("label", d["label"] or None), ("meeting_id", m["id"]),
                ("time", m["time"]), ("venue", m["venue"]), ("in_repo", d["in_repo"])] + ([("mentions", d["mentions"])] if d.get("mentions") else [])))
    for f in loose:
        if not f.get("matched"):
            docs_out.append(OrderedDict([("date", None), ("meeting", None), ("category", "Other"),
                                         ("filename", f["filename"]), ("path", f["path"]),
                                         ("url", PAGES + f["path"]), ("repo_url", PAGES + f["path"]),
                                         ("escribe_doc_id", None), ("escribe_url", None), ("label", None),
                                         ("meeting_id", None), ("time", None), ("venue", None), ("in_repo", True)]))
    json.dump(docs_out, open(os.path.join(a.out, "documents.json"), "w"), ensure_ascii=False, separators=(",", ":"))

    topics = OrderedDict((t, []) for t in TOPICS)
    for m in ordered:
        for it in m["items"]:
            for t in it.get("topics", []):
                topics[t].append(OrderedDict([("date", m["date"]), ("meeting", m["meeting"]), ("meeting_id", m["id"]),
                                              ("item", it["number"]), ("title", it["title"]), ("page", it["page"])]))
    json.dump(topics, open(os.path.join(a.out, "topics.json"), "w"), ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(a.out, "missing-urls.txt"), "w") as fh:
        for m in ordered:
            for d in m["documents"]:
                if not d["in_repo"] and d["escribe_url"]:
                    fh.write(d["escribe_url"] + "\n")

    # coverage
    cov = defaultdict(lambda: [0, 0, 0, 0])  # meetings, docs, in_repo, docs without link
    for m in ordered:
        key = (m["meeting"], m["date"][:4])
        cov[key][0] += 1
        for d in m["documents"]:
            cov[key][1] += 1
            cov[key][2] += d["in_repo"]
            cov[key][3] += (not d["in_repo"] and not d["escribe_url"])
    with open(os.path.join(a.out, "COVERAGE.md"), "w") as fh:
        tot = [sum(v[i] for v in cov.values()) for i in range(4)]
        fh.write(f"# Catalogue coverage\n\nBuilt {dt.date.today().isoformat()}: {len(ordered)} meetings, "
                 f"{tot[1]} documents listed, {tot[2]} in the repo, {tot[1] - tot[2]} not yet mirrored "
                 f"({tot[3]} of those have no eScribe link in the listings yet).\n\n"
                 "| Meeting type | Year | Meetings | Docs | In repo | Missing | No link |\n|---|---|---|---|---|---|---|\n")
        for (mtg, yr), v in sorted(cov.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=False):
            fh.write(f"| {mtg} | {yr} | {v[0]} | {v[1]} | {v[2]} | {v[1] - v[2]} | {v[3]} |\n")
        if any(not f.get("matched") for f in loose):
            fh.write("\n## Repo files not linked to a meeting\n\n")
            for f in loose:
                if not f.get("matched"):
                    fh.write(f"- {f['path']}\n")
    if a.write_repo_index:
        write_repo_index(a, ordered, docs_out, topics)

    n_items = sum(len(m["items"]) for m in ordered)
    print(f"{len(ordered)} meetings, {len(docs_out)} documents, {n_items} agenda items -> {a.out}")


def _slim(x):
    o = OrderedDict([("date", x["date"]), ("meeting", x["meeting"]), ("category", x["category"]),
                     ("filename", x["filename"]), ("path", x["path"]), ("url", x["url"])])
    if x.get("meeting_id"):
        o["meeting_id"] = x["meeting_id"]
    if x.get("escribe_doc_id"):
        o["escribe_id"] = int(x["escribe_doc_id"])
    if x.get("mentions"):
        o["mentions"] = x["mentions"]
    return o


def write_repo_index(a, ordered, docs_out, topics):
    """Files the WDC-analyst GPT reads (via data.json), kept small enough for an Action response."""
    compact = dict(ensure_ascii=False, separators=(",", ":"))
    repo = a.repo
    # index.json: every mirrored file, same core fields as before plus eScribe id, meeting id and mentions
    index = [_slim(x) for x in docs_out if x["in_repo"]]
    with open(os.path.join(repo, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, **compact)
    # minutes.json: every set of minutes we can link to, mirrored copy first, eScribe as fallback
    mins = []
    for x in docs_out:
        if x["category"] != "Minutes" or not x["url"]:
            continue
        urls = []
        if x["in_repo"]:
            urls.append({"url": x["url"], "role": "primary"})
        if x.get("escribe_url"):
            urls.append({"url": x["escribe_url"], "role": "escribe" if x["in_repo"] else "primary"})
        mins.append(OrderedDict([("date", x["date"]), ("meeting", x["meeting"]), ("category", "Minutes"),
                                 ("filename", x["filename"]), ("path", x["path"]), ("meeting_id", x["meeting_id"]),
                                 ("urls", urls)]))
    with open(os.path.join(repo, "minutes.json"), "w", encoding="utf-8") as fh:
        json.dump(mins, fh, **compact)
    # catalog/meetings/YYYY.json: full meeting records (documents, agenda items, topics), one file per year
    mdir = os.path.join(a.out, "meetings")
    os.makedirs(mdir, exist_ok=True)
    years = defaultdict(list)
    for m in ordered:
        years[m["date"][:4]].append(OrderedDict([(k, m[k]) for k in
                                                 ("id", "meeting", "date", "time", "venue", "topics", "items", "documents")]))
    for y, ms in years.items():
        with open(os.path.join(mdir, f"{y}.json"), "w", encoding="utf-8") as fh:
            json.dump(ms, fh, **compact)
    # data.json: the list of sources the GPT Action loads
    rel_out = os.path.relpath(a.out, repo).replace(os.sep, "/")
    rel_out = "" if rel_out == "." else rel_out + "/"
    sources = [
        {"name": "index", "description": "Every document mirrored in this repo", "url": a.raw_base + "index.json"},
        {"name": "minutes", "description": "Every set of minutes, mirrored or on eScribe", "url": a.raw_base + "minutes.json"},
        {"name": "topics", "description": "Topic -> agenda items across all meetings", "url": a.raw_base + rel_out + "topics.json"},
    ] + [{"name": f"meetings-{y}", "description": f"All {y} meetings: documents, agenda items, topics",
          "url": a.raw_base + rel_out + f"meetings/{y}.json"} for y in sorted(years, reverse=True)]
    with open(os.path.join(repo, "data.json"), "w", encoding="utf-8") as fh:
        json.dump({"sources": sources}, fh, indent=2)


if __name__ == "__main__":
    main()
