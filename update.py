#!/usr/bin/env python3
"""Haalt de RSS-feeds van vier Vlaamse nieuwssites op, filtert op Vlaams
onderwijsnieuws, bundelt dubbels en schrijft het resultaat naar data.json."""
import json, re, sys, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BRUSSEL = ZoneInfo("Europe/Brussels")
HIER = Path(__file__).parent
DATA = HIER / "data.json"
NEGEER = HIER / "negeer.txt"      # één URL per regel die je nooit wilt zien
BEWAAR_DAGEN = 90

# Per site meerdere kandidaat-feeds: wat niet werkt wordt gelogd en overgeslagen.
# Controleer na de eerste run in het Actions-log welke feeds echt antwoorden.
FEEDS = {
    "VRT NWS":    ["https://www.vrt.be/vrtnws/nl.rss.articles.xml"],
    "HLN":        ["https://www.hln.be/rss.xml", "https://www.hln.be/binnenland/rss.xml"],
    "Nieuwsblad": ["https://www.nieuwsblad.be/binnenland/rss"],
    "GVA":        ["https://www.gva.be/binnenland/rss"],
}

INCLUDE = re.compile(
    r"onderwijs|leerkracht|leraar|leraren|lerares|lerarentekort|lerarenopleiding|"
    r"school|scholen|schooljaar|leerling|leerplicht|eindtermen|studiedag|"
    r"zomervakantie|schoolvakantie|hogeschool|kleuter|studiekeuze|inspectie", re.I)
# Alleen op de TITEL toegepast: Franstalig/buitenlands onderwijs hoort er niet bij.
EXCLUDE_TITEL = re.compile(
    r"franstalig|waals|wallonië|federatie wallonië|fwb|franse gemeenschap|"
    r"indiase|amerikaanse|nederlandse scholen|tweede kamer|oekraïn|gaza|"
    r"oude school|old school|schoolslag", re.I)

# Beleids- en systeemniveau: een artikel moet minstens één van deze woorden hebben
# (titel of samenvatting), anders is het te lokaal of te vrijblijvend.
LANDELIJK = re.compile(
    r"minister|demir|regering|parlement|vlaams|vlaanderen|beleid|hervorm|besparing|"
    r"staking|staakt|vakbond|acod|acv|cgsp|aloc|onderwijsnet|katholiek onderwijs|\bgo!|"
    r"pisa|eindtermen|lerarentekort|leerplicht|inspectie|decreet|akkoord|"
    r"schooluitval|studietoelage|tekort|enquête|rapport|cijfers|onderzoek", re.I)

# Typisch lokaal of feelgood: nooit opnemen als het in de titel staat.
LOKAAL_TITEL = re.compile(
    r"\bjuf\b|\bjuffrouw\b|\bmeester\b|gaat op pensioen|met pensioen|afscheid|"
    r"jubileum|huldig|schoolfeest|inzamelactie|opendeurdag|speelplaats|kermis|"
    r"schoolreis|nieuwbouw|eerste steen", re.I)

# Regionale rubrieken in de URL (HLN, Nieuwsblad, GVA). Pas gerust aan.
REGIO_URL = re.compile(
    r"^https?://[^/]+/(regio|lokaal|lokaal-nieuws|gemeenten|antwerpen|gent|brugge|leuven|"
    r"mechelen|hasselt|kortrijk|oostende|aalst|sint-niklaas|waasland|kempen|limburg|"
    r"vlaams-brabant|oost-vlaanderen|west-vlaanderen|brussel)(/|$)", re.I)

STOP = set("""dat deze naar voor met van het een zijn haar hun niet ook maar meer wordt worden over
door bij uit als dan wel geen tot nog zich ze zij wij jij hebben heeft had zal zou kan kunnen
alle veel nieuwe nieuw gaat gaan krijgt krijgen wil willen zegt volgens""".split())


def schoon(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def norm_url(u):
    return re.sub(r"[?#].*$", "", u.strip()).rstrip("/")


def lokaal(tag):
    return tag.rsplit("}", 1)[-1]


def lees_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "OnderwijsInHetNieuwsBot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    for el in root.iter():
        if lokaal(el.tag) not in ("item", "entry"):
            continue
        d = {lokaal(c.tag): c for c in el}
        titel = schoon(d["title"].text) if "title" in d else ""
        link = ""
        if "link" in d:
            link = (d["link"].text or d["link"].attrib.get("href", "")).strip()
        bron = next((d[k] for k in ("description", "summary", "encoded", "content") if k in d), None)
        samen = schoon(bron.text) if bron is not None else ""
        raw = None
        for k in ("pubDate", "published", "updated", "date"):
            if k in d and d[k].text:
                raw = d[k].text.strip(); break
        yield titel, link, samen, raw


def parse_datum(raw):
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRUSSEL).date().isoformat()


def is_onderwijs(titel, samenvatting, url=""):
    if EXCLUDE_TITEL.search(titel) or LOKAAL_TITEL.search(titel) or REGIO_URL.search(url):
        return False
    if not LANDELIJK.search(titel + " " + samenvatting):
        return False
    if INCLUDE.search(titel):
        return True
    return len({m.lower()[:6] for m in INCLUDE.findall(samenvatting)}) >= 2


def kern(titel):
    woorden = re.findall(r"[a-zà-ÿ0-9]+", titel.lower())
    return {w[:6] for w in woorden if len(w) >= 4 and w not in STOP}


def zelfde_onderwerp(a, b):
    da = datetime.fromisoformat(a["datum"]); db = datetime.fromisoformat(b["datum"])
    if abs((da - db).days) > 3:
        return False
    ka, kb = kern(a["titel"]), kern(b["titel"])
    if not ka or not kb:
        return False
    deel = len(ka & kb)
    score = deel / min(len(ka), len(kb))
    return (deel >= 3 and score >= 0.5) or (deel >= 2 and score >= 0.67)


def bundel(artikels):
    groepen = []
    for art in sorted(artikels, key=lambda a: (a["datum"], a["site"])):
        for g in groepen:
            if any(zelfde_onderwerp(art, x) for x in g):
                g.append(art); break
        else:
            groepen.append([art])
    onderwerpen = []
    for g in groepen:
        voorkeur = sorted(g, key=lambda a: (a["site"] != "VRT NWS", a["datum"]))[0]
        links, gezien = [], set()
        for a in sorted(g, key=lambda a: a["datum"]):
            if a["site"] not in gezien:
                gezien.add(a["site"])
                links.append({"site": a["site"], "url": a["url"], "titel": a["titel"]})
        onderwerpen.append({"datum": max(a["datum"] for a in g),
                            "onderwerp": voorkeur["titel"], "links": links})
    return sorted(onderwerpen, key=lambda o: o["datum"], reverse=True)


def main():
    bestaand = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    artikels = {norm_url(a["url"]): a for a in bestaand.get("artikels", [])
                if is_onderwijs(a["titel"], "", a["url"])}   # oude data opnieuw toetsen
    negeer = {norm_url(l) for l in NEGEER.read_text().split()} if NEGEER.exists() else set()
    nieuw, gelukt = 0, 0

    for site, urls in FEEDS.items():
        for url in urls:
            try:
                n = 0
                for titel, link, samen, raw in lees_feed(url):
                    datum = parse_datum(raw)
                    if not (titel and link and datum) or norm_url(link) in negeer:
                        continue
                    if not is_onderwijs(titel, samen, link):
                        continue
                    key = norm_url(link)
                    if key not in artikels:
                        artikels[key] = {"site": site, "titel": titel, "url": link, "datum": datum}
                        nieuw += 1; n += 1
                gelukt += 1
                print(f"OK    {site:11} {url}  (+{n})")
            except Exception as e:
                print(f"FOUT  {site:11} {url}  -> {type(e).__name__}: {e}")

    if gelukt == 0:
        print("Geen enkele feed werkte; data.json blijft ongewijzigd.")
        sys.exit(1)

    grens = (datetime.now(BRUSSEL) - timedelta(days=BEWAAR_DAGEN)).date().isoformat()
    lijst = [a for k, a in artikels.items() if a["datum"] >= grens and k not in negeer]
    out = {"bijgewerkt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "artikels": sorted(lijst, key=lambda a: a["datum"], reverse=True),
           "onderwerpen": bundel(lijst)[:20]}
    DATA.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Klaar: {nieuw} nieuwe artikels, {len(out['onderwerpen'])} onderwerpen.")


if __name__ == "__main__":
    main()
