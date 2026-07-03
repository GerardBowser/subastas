#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rastreador de subastas de inmuebles en Cataluña a partir del BOE.

Fuente: API oficial de datos abiertos del BOE (sin scraping del portal, que
bloquea robots).
  https://boe.es/datosabiertos/api/boe/sumario/AAAAMMDD   (sumario diario)

Estrategia (v2):
  1. Recorre los sumarios de los últimos DIAS_ATRAS días.
  2. Candidata = anuncio que contiene "subasta" Y menciona una provincia o
     municipio catalán (diccionario amplio) en su título/epígrafe.
  3. Solo para esas candidatas descarga el edicto (XML) y extrae lo que puede
     (importe, tasación, cargas, ref. catastral, municipio, dirección...).
  4. Descarta lo que claramente no es inmueble (vehículos, maquinaria...).
  5. Enriquece con Catastro si hay referencia catastral.
  6. Estima venta/alquiler (PROVISIONAL por €/m² de zona; ver TODO en valorar()).
  7. Fusiona con lo ya conocido y escribe docs/data.json.

Limitaciones honestas: las administrativas (AEAT/ATC) remiten al portal para las
cifras económicas y esas pueden quedar a null ("por confirmar"). Las judiciales
suelen traer el detalle en el edicto y se parsean mejor.
"""

import json, re, sys, time, unicodedata, urllib.request, urllib.error
from datetime import date, timedelta, datetime
from pathlib import Path
import xml.etree.ElementTree as ET

DIAS_ATRAS   = 21                       # ventana de sumarios a revisar
VENTANA_VIDA = 60                       # días que mantenemos una subasta en el panel
MAX_DOCS     = 400                      # tope de edictos a descargar por ejecución
OUT          = Path(__file__).parent / "docs" / "data.json"
UA           = "subastas-catalunya/2.0 (uso personal)"

# --- Municipios/provincias de Cataluña (clave normalizada -> provincia) --------
_MUNIS = {
 "Barcelona":"Barcelona","L'Hospitalet de Llobregat":"Barcelona","Badalona":"Barcelona",
 "Terrassa":"Barcelona","Sabadell":"Barcelona","Mataró":"Barcelona",
 "Santa Coloma de Gramenet":"Barcelona","Cornellà de Llobregat":"Barcelona",
 "Sant Cugat del Vallès":"Barcelona","Sant Boi de Llobregat":"Barcelona","Rubí":"Barcelona",
 "Manresa":"Barcelona","Vilanova i la Geltrú":"Barcelona","Viladecans":"Barcelona",
 "Castelldefels":"Barcelona","Granollers":"Barcelona","Cerdanyola del Vallès":"Barcelona",
 "Mollet del Vallès":"Barcelona","Esplugues de Llobregat":"Barcelona","Gavà":"Barcelona",
 "El Prat de Llobregat":"Barcelona","Igualada":"Barcelona","Vic":"Barcelona",
 "Vilafranca del Penedès":"Barcelona","Ripollet":"Barcelona","Montcada i Reixac":"Barcelona",
 "Sant Adrià de Besòs":"Barcelona","Barberà del Vallès":"Barcelona",
 "Sant Feliu de Llobregat":"Barcelona","Premià de Mar":"Barcelona","Sant Joan Despí":"Barcelona",
 "Martorell":"Barcelona","Sant Vicenç dels Horts":"Barcelona","Cardedeu":"Barcelona",
 "Manlleu":"Barcelona","Sitges":"Barcelona","Molins de Rei":"Barcelona","Vilassar de Mar":"Barcelona",
 "Girona":"Girona","Figueres":"Girona","Blanes":"Girona","Lloret de Mar":"Girona","Olot":"Girona",
 "Salt":"Girona","Palafrugell":"Girona","Sant Feliu de Guíxols":"Girona","Banyoles":"Girona",
 "Roses":"Girona","Santa Cristina d'Aro":"Girona","Palamós":"Girona","Ripoll":"Girona",
 "Lleida":"Lleida","Balaguer":"Lleida","Tàrrega":"Lleida","Mollerussa":"Lleida",
 "La Seu d'Urgell":"Lleida","Cervera":"Lleida","Solsona":"Lleida","Sanaüja":"Lleida",
 "Tarragona":"Tarragona","Reus":"Tarragona","Tortosa":"Tarragona","Cambrils":"Tarragona",
 "El Vendrell":"Tarragona","Valls":"Tarragona","Salou":"Tarragona","Amposta":"Tarragona",
 "Calafell":"Tarragona","Vila-seca":"Tarragona","Sant Carles de la Ràpita":"Tarragona",
 "L'Hospitalet de l'Infant":"Tarragona","Castellet i la Gornal":"Barcelona",
}
_PROV = {"Barcelona":"Barcelona","Girona":"Girona","Gerona":"Girona","Lleida":"Lleida",
         "Lérida":"Lleida","Tarragona":"Tarragona","Cataluña":"Barcelona","Catalunya":"Barcelona"}

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii","ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+"," ", s).strip()

# índices normalizados, municipios con prioridad sobre provincias/genéricos
_IDX_MUNI = sorted([(norm(n), n, p) for n, p in _MUNIS.items()], key=lambda x: -len(x[0]))
_IDX_PROV = sorted([(norm(n), n, p) for n, p in _PROV.items()],  key=lambda x: -len(x[0]))

def match_cat(texto):
    t = norm(texto)
    for grupo in (_IDX_MUNI, _IDX_PROV):          # municipios primero
        for clave, nombre, prov in grupo:
            if re.search(r"\b"+re.escape(clave)+r"\b", t):
                return nombre, prov
    return None, None

KW_INMUEBLE = ["vivienda","piso","casa","chalet","ático","atico","dúplex","duplex","local",
               "garaje","aparcamiento","trastero","solar","finca","nave","parcela","inmueble",
               "urbana","rústica","rustica","edificio","apartamento","vivienda unifamiliar"]
KW_NO_INMUEBLE = ["vehículo","vehiculo","turismo matrícula","embarcación","embarcacion",
                  "maquinaria","motocicleta","remolque","aeronave"]
TIPO_MAP = [("garaje","Garaje"),("aparcamiento","Garaje"),("trastero","Trastero"),
            ("local","Local"),("nave","Nave"),("solar","Solar"),("parcela","Solar"),
            ("edificio","Edificio"),("vivienda","Vivienda"),("piso","Vivienda"),
            ("apartamento","Vivienda"),("casa","Vivienda"),("chalet","Vivienda"),
            ("ático","Vivienda"),("dúplex","Vivienda")]

ZONA_EURM2 = {"Barcelona":4200,"Girona":2100,"Lleida":1500,"Tarragona":1900}
ZONA_EURM2_DEF = 1600


def http_get(url, accept="application/xml", timeout=30):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def fetch_sumario(d: date):
    url = f"https://boe.es/datosabiertos/api/boe/sumario/{d:%Y%m%d}"
    try:
        return ET.fromstring(http_get(url))
    except urllib.error.HTTPError as e:
        if e.code in (404, 400): return None
        print(f"  aviso: sumario {d} -> HTTP {e.code}", file=sys.stderr); return None
    except Exception as e:
        print(f"  aviso: sumario {d} -> {e}", file=sys.stderr); return None

def _ln(tag):
    return tag.split("}")[-1].lower()   # nombre local, ignora namespaces

def _child_text(node, name):
    for ch in node:
        if _ln(ch.tag) == name:
            return (ch.text or "").strip()
    return ""

def walk_items(root):
    def rec(node, seccion, departamento, epigrafe):
        tag = _ln(node.tag)
        if tag == "seccion": seccion = node.get("nombre", seccion)
        elif tag == "departamento": departamento = node.get("nombre", departamento)
        elif tag == "epigrafe": epigrafe = node.get("nombre", epigrafe)
        elif tag == "item":
            yield {"id": node.get("id",""),
                   "titulo": _child_text(node,"titulo"),
                   "url_xml": _child_text(node,"url_xml"),
                   "seccion": seccion, "departamento": departamento, "epigrafe": epigrafe}
        for ch in list(node):
            yield from rec(ch, seccion, departamento, epigrafe)
    yield from rec(root, "", "", "")

def es_candidata(it):
    """Devuelve (muni, prov) si el anuncio parece subasta en Cataluña; si no, None."""
    blob = f"{it['titulo']} {it['epigrafe']} {it['departamento']}"
    if "subasta" not in norm(blob):
        return None
    muni, prov = match_cat(blob)
    if not prov:
        return None
    return muni, prov

def tipo_de(texto):
    t = texto.lower()
    for k, v in TIPO_MAP:
        if k in t: return v
    return "Inmueble"

NUM = r"([\d\.]+(?:,\d+)?)"
def to_num(s):
    if not s: return None
    s = s.replace(".","").replace(",",".")
    try: return round(float(s))
    except: return None
def buscar(patrones, texto):
    for p in patrones:
        m = re.search(p, texto, re.I)
        if m: return to_num(m.group(1))
    return None

def parse_documento(url_xml):
    det = {"m2":None,"anyo":None,"valorSubasta":None,"salida":None,"deposito":None,
           "tramo":None,"cargas":0,"cat":None,"addr":None,"muni":None,
           "pos":"Desconocida","cuota":100,"viviendaHabitual":False,"visitable":None,
           "ini":None,"fin":None,"flags":[]}
    txt = ""
    if not url_xml: return det, txt
    try:
        raw = http_get(url_xml).decode("utf-8","ignore")
    except Exception as e:
        print(f"    aviso: doc {url_xml} -> {e}", file=sys.stderr); return det, txt
    txt = re.sub(r"\s+"," ", re.sub(r"<[^>]+>"," ", raw))
    low = txt.lower()

    det["valorSubasta"] = buscar([r"valor de (?:la )?subasta[^\d]{0,20}"+NUM,
                                  r"valor de tasaci[oó]n[^\d]{0,20}"+NUM], txt)
    det["salida"]       = buscar([r"(?:importe|cantidad|tipo|valor de salida|puja m[ií]nima)[^\d]{0,25}"+NUM], txt)
    det["deposito"]     = buscar([r"dep[oó]sito[^\d]{0,25}"+NUM], txt)
    det["tramo"]        = buscar([r"tramos?[^\d]{0,20}"+NUM], txt)
    c = buscar([r"cargas?[^\d]{0,40}"+NUM], txt); det["cargas"] = c if c is not None else 0

    m = re.search(r"\b([0-9A-Z]{7}[0-9A-Z]{7}[0-9A-Z]{4}[0-9A-Z]{2})\b", raw)
    if m: det["cat"] = m.group(1)
    m = re.search(r"(\d{2,4})\s*(?:m2|m²|metros cuadrados)", low)
    if m:
        v = int(m.group(1)); det["m2"] = v if 5 <= v <= 5000 else None

    if "ocupad" in low: det["pos"] = "Ocupado"
    elif "sin posesi" in low or "no consta poseedor" in low: det["pos"] = "Sin posesión"
    elif "libre de ocupantes" in low or "sin ocupantes" in low: det["pos"] = "Libre"
    if "vivienda habitual" in low: det["viviendaHabitual"] = True
    mm = re.search(r"(\d{1,3})\s*%\s*(?:indivis|de la finca|del pleno)", low)
    if ("indivis" in low or "mitad indivisa" in low) and mm and int(mm.group(1)) < 100:
        det["cuota"] = int(mm.group(1))
        det["flags"].append({"lvl":"alto","txt":f"Se subasta el {mm.group(1)}% de la finca (proindiviso)."})
    if "no se puede visitar" in low or "tapiad" in low: det["visitable"] = False

    # dirección aproximada (calle/carrer/avda ... número)
    m = re.search(r"((?:calle|c/|carrer|avda|avenida|av\.|plaza|pla[çc]a|passeig|paseo|rambla|ronda)"
                  r"[^,;.]{2,60}?\d{1,4}[^,;.]{0,20})", txt, re.I)
    if m: det["addr"] = m.group(1).strip()[:90]
    # municipio más preciso del propio texto
    muni2, _ = match_cat(txt)
    if muni2: det["muni"] = muni2
    return det, txt


def enrich_catastro(cat):
    if not cat: return {}
    url = ("https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/"
           f"Consulta_DNPRC?RefCat={cat}")
    try:
        s = json.dumps(json.loads(http_get(url, accept="application/json", timeout=20)))
        out = {}
        m = re.search(r'"sfc":\s*"?(\d+)', s)
        if m: out["m2"] = int(m.group(1))
        m = re.search(r'"ant":\s*"?(\d{4})', s)
        if m: out["anyo"] = int(m.group(1))
        return out
    except Exception:
        return {}

def valorar(rec):
    """PLACEHOLDER. TODO: sustituir por comparables reales de venta/alquiler."""
    eurm2 = ZONA_EURM2.get(rec.get("prov"), ZONA_EURM2_DEF)
    if rec.get("muni") == "Barcelona": eurm2 = ZONA_EURM2["Barcelona"]
    if rec.get("m2"):
        rec.setdefault("mercadoEst", round(rec["m2"] * eurm2))
        rec.setdefault("alquilerEst", round(rec["m2"] * eurm2 * 0.004))
    else:
        rec.setdefault("mercadoEst", None); rec.setdefault("alquilerEst", 0)
    return rec

def cargar_existentes():
    if OUT.exists():
        try:
            return {a["id"]: a for a in json.loads(OUT.read_text("utf-8")).get("auctions",[])}
        except Exception:
            pass
    return {}


def main():
    conocidas = cargar_existentes()
    # descartamos las semilla en cuanto empieza a haber reales
    hoy = date.today()
    nuevas = 0; docs = 0
    # --- diagnóstico: embudo de filtrado ---
    n_dias = 0; n_items = 0; n_subasta = 0; n_cat = 0; n_inmueble = 0
    n_repe = 0; n_mueble = 0
    for i in range(DIAS_ATRAS):
        d = hoy - timedelta(days=i)
        root = fetch_sumario(d)
        if root is None: continue
        n_dias += 1
        for it in walk_items(root):
            n_items += 1
            blob = f"{it['titulo']} {it['epigrafe']} {it['departamento']}"
            if "subasta" in norm(blob): n_subasta += 1
            cand = es_candidata(it)
            if not cand: continue
            n_cat += 1
            if it["id"] in conocidas: n_repe += 1; continue
            if docs >= MAX_DOCS: break
            muni, prov = cand
            det, txt = parse_documento(it["url_xml"]); docs += 1; time.sleep(0.25)

            # Criterio: una subasta en Cataluña se ACEPTA por defecto; solo se
            # descarta si es CLARAMENTE un bien mueble (vehículo, maquinaria…).
            base = (it["titulo"] + " " + txt).lower()
            hay_inmueble = any(k in base for k in KW_INMUEBLE)
            es_mueble    = any(k in base for k in KW_NO_INMUEBLE)
            if es_mueble and not hay_inmueble:
                n_mueble += 1; continue
            n_inmueble += 1

            rec = {
                "id": it["id"],
                "org": it["departamento"] or "BOE",
                "tipo": tipo_de(it["titulo"]+" "+txt[:400]),
                "addr": det.get("addr") or (it["titulo"][:90] if it["titulo"] else "—"),
                "muni": det.get("muni") or muni or prov,
                "prov": prov,
                "boe_xml": it["url_xml"],
                "first_seen": hoy.isoformat(),
                **{k:v for k,v in det.items() if k not in ("addr","muni")},
            }
            rec = valorar(rec)
            conocidas[rec["id"]] = rec
            nuevas += 1
            print(f"  + {rec['id']}  {rec['tipo']:9} {rec['muni']}")
        if docs >= MAX_DOCS:
            print("  (alcanzado MAX_DOCS)", file=sys.stderr); break

    print(f"DIAG · dias con BOE: {n_dias} · anuncios totales: {n_items} · "
          f"con 'subasta': {n_subasta} · subastas en Cataluña: {n_cat} · "
          f"repetidas/ya vistas: {n_repe} · descartadas por mueble: {n_mueble} · "
          f"inmuebles válidos: {n_inmueble}")
    if n_items == 0:
        print("DIAG · ¡0 anuncios leídos! Puede que la estructura del sumario no se "
              "esté parseando: avísame con este log.", file=sys.stderr)

    # si ya hay reales, quitamos las de ejemplo (id que empieza por SUB- y source seed)
    hay_reales = any(not k.startswith("SUB-") or v.get("boe_xml") for k,v in conocidas.items())
    if hay_reales:
        conocidas = {k:v for k,v in conocidas.items() if v.get("boe_xml") or v.get("first_seen")}

    vivas = {}
    for k, a in conocidas.items():
        fin = a.get("fin")
        if fin:
            try:
                if datetime.fromisoformat(fin).date() < hoy: continue
            except Exception: pass
        fs = a.get("first_seen")
        if fs:
            try:
                if (hoy - datetime.fromisoformat(fs).date()).days > VENTANA_VIDA: continue
            except Exception: pass
        vivas[k] = a

    src = "BOE datos abiertos" if nuevas or any(v.get("boe_xml") for v in vivas.values()) else "seed (datos de ejemplo)"
    OUT.write_text(json.dumps(
        {"generated": hoy.isoformat(), "source": src, "count": len(vivas),
         "auctions": list(vivas.values())}, ensure_ascii=False, indent=1), "utf-8")
    print(f"OK · {nuevas} nuevas · {docs} edictos leídos · {len(vivas)} activas · escrito {OUT}")


if __name__ == "__main__":
    main()
