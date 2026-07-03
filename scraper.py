#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rastreador de subastas de inmuebles en Cataluña a partir del BOE.

Fuente: API oficial de datos abiertos del BOE (sin scraping del portal, que
bloquea robots). Documento por documento se descarga su XML del diario.

  https://boe.es/datosabiertos/api/boe/sumario/AAAAMMDD   (sumario diario)

Qué hace:
  1. Recorre los sumarios de los últimos DIAS_ATRAS días.
  2. Se queda con los anuncios de SUBASTA de INMUEBLE en CATALUÑA.
  3. Extrae del texto lo que puede (importe, tasación, cargas, ref. catastral...).
  4. Enriquece con Catastro (superficie/año/uso) si hay referencia catastral.
  5. Estima valor de venta y alquiler (PROVISIONAL: por €/m² de zona; ver TODO).
  6. Fusiona con lo ya conocido y escribe docs/data.json (lo que lee el dashboard).

Limitaciones honestas:
  - Las subastas ADMINISTRATIVAS (AEAT/ATC) remiten al portal para las cifras
    económicas, que no son accesibles de forma automatizada. En esos casos los
    importes quedan a null y el dashboard los marca como "por confirmar".
  - Las JUDICIALES suelen traer el detalle en el edicto y se parsean mejor.
  - La valoración de mercado es un PLACEHOLDER hasta enchufar comparables reales.
"""

import json, re, sys, time, urllib.request, urllib.error
from datetime import date, timedelta, datetime
from pathlib import Path
import xml.etree.ElementTree as ET

DIAS_ATRAS   = 14                       # ventana de sumarios a revisar cada ejecución
VENTANA_VIDA = 45                       # días que mantenemos una subasta en el panel
OUT          = Path(__file__).parent / "docs" / "data.json"
UA           = "subastas-catalunya/1.0 (uso personal; contacto: tu-email)"

PROV_CAT = {
    "barcelona":"Barcelona", "girona":"Girona", "gerona":"Girona",
    "lleida":"Lleida", "lérida":"Lleida", "lerida":"Lleida",
    "tarragona":"Tarragona",
}
KW_INMUEBLE = ["vivienda","piso","casa","chalet","chalé","ático","atico","dúplex","duplex",
               "local","garaje","plaza de aparcamiento","aparcamiento","trastero","solar",
               "finca","nave","parcela","inmueble","urbana","rústica","rustica","edificio"]
TIPO_MAP = [("garaje","Garaje"),("aparcamiento","Garaje"),("trastero","Trastero"),
            ("local","Local"),("nave","Nave"),("solar","Solar"),("parcela","Solar"),
            ("edificio","Edificio"),("vivienda","Vivienda"),("piso","Vivienda"),
            ("casa","Vivienda"),("chalet","Vivienda"),("ático","Vivienda"),("dúplex","Vivienda")]

# €/m² de venta orientativos por zona (PROVISIONAL). El histórico real los sustituye.
ZONA_EURM2 = {"Barcelona":4200,"Girona":2100,"Lleida":1500,"Tarragona":1900}
ZONA_EURM2_DEF = 1600


def http_get(url, accept="application/xml", timeout=30):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_sumario(d: date):
    """Devuelve la raíz XML del sumario de un día, o None si no hay diario."""
    url = f"https://boe.es/datosabiertos/api/boe/sumario/{d:%Y%m%d}"
    try:
        return ET.fromstring(http_get(url))
    except urllib.error.HTTPError as e:
        if e.code in (404, 400):      # día sin BOE (festivo/domingo)
            return None
        print(f"  aviso: sumario {d} -> HTTP {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  aviso: sumario {d} -> {e}", file=sys.stderr)
        return None


def walk_items(root):
    """Recorre el árbol del sumario arrastrando el contexto de sección/departamento.
    Emite dicts {id, titulo, url_xml, seccion, departamento}."""
    def rec(node, seccion, departamento):
        tag = node.tag.lower()
        if tag == "seccion":
            seccion = node.get("nombre", seccion)
        elif tag == "departamento":
            departamento = node.get("nombre", departamento)
        elif tag == "item":
            yield {
                "id": node.get("id",""),
                "titulo": (node.findtext("titulo") or "").strip(),
                "url_xml": (node.findtext("url_xml") or "").strip(),
                "seccion": seccion, "departamento": departamento,
            }
        for ch in list(node):
            yield from rec(ch, seccion, departamento)
    yield from rec(root, "", "")


def es_subasta_inmueble_cataluna(it):
    blob = f"{it['titulo']} {it['departamento']} {it['seccion']}".lower()
    if "subasta" not in blob and "subastas" not in it["departamento"].lower():
        return None
    if not any(k in blob for k in KW_INMUEBLE):
        return None
    prov = None
    for k, v in PROV_CAT.items():
        if k in blob:
            prov = v; break
    if "cataluña" in blob or "catalunya" in blob:
        prov = prov or "Barcelona"
    if not prov:
        return None
    return prov


def tipo_de(texto):
    t = texto.lower()
    for k, v in TIPO_MAP:
        if k in t:
            return v
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
    """Descarga el XML del anuncio y extrae lo posible. Devuelve dict parcial."""
    out = {"m2":None,"anyo":None,"valorSubasta":None,"salida":None,"deposito":None,
           "tramo":None,"cargas":None,"cat":None,"addr":None,"muni":None,
           "pos":"Desconocida","cuota":100,"viviendaHabitual":False,"visitable":None,
           "ini":None,"fin":None,"flags":[]}
    if not url_xml: return out
    try:
        raw = http_get(url_xml).decode("utf-8","ignore")
    except Exception as e:
        print(f"    aviso: doc {url_xml} -> {e}", file=sys.stderr); return out
    txt = re.sub(r"<[^>]+>"," ", raw)          # texto plano del edicto
    txt = re.sub(r"\s+"," ", txt)

    out["valorSubasta"] = buscar([r"valor de (?:la )?subasta[^\d]{0,20}"+NUM,
                                  r"valor de tasaci[oó]n[^\d]{0,20}"+NUM], txt)
    out["salida"]       = buscar([r"(?:importe|cantidad|tipo|puja m[ií]nima|valor de salida)[^\d]{0,25}"+NUM], txt)
    out["deposito"]     = buscar([r"dep[oó]sito[^\d]{0,25}"+NUM], txt)
    out["tramo"]        = buscar([r"tramos?[^\d]{0,20}"+NUM,
                                  r"puja m[ií]nima[^\d]{0,20}"+NUM], txt)
    cargas              = buscar([r"cargas?[^\d]{0,40}"+NUM], txt)
    out["cargas"]       = cargas if cargas is not None else 0

    m = re.search(r"\b([0-9A-Z]{7}[0-9A-Z]{7}[0-9A-Z]{4}[0-9A-Z]{2})\b", raw)
    if m: out["cat"] = m.group(1)
    m = re.search(r"(\d{2,4})\s*(?:m2|m²|metros cuadrados)", txt, re.I)
    if m:
        v = int(m.group(1));  out["m2"] = v if 5 <= v <= 5000 else None

    low = txt.lower()
    if "ocupad" in low: out["pos"] = "Ocupado"
    elif "sin posesi" in low or "no consta poseedor" in low: out["pos"] = "Sin posesión"
    elif "libre de ocupantes" in low or "sin ocupantes" in low: out["pos"] = "Libre"
    if "vivienda habitual" in low: out["viviendaHabitual"] = True
    if "indivis" in low or "mitad indivisa" in low or "porcentaje" in low:
        mm = re.search(r"(\d{1,3})\s*%", txt)
        if mm and int(mm.group(1)) < 100:
            out["cuota"] = int(mm.group(1))
            out["flags"].append({"lvl":"alto","txt":f"Se subasta el {mm.group(1)}% de la finca (proindiviso)."})
    if "no se puede visitar" in low or "tapiad" in low: out["visitable"] = False
    return out


def enrich_catastro(cat):
    """Best-effort: superficie/año/uso desde el Catastro. Silencioso si falla."""
    if not cat: return {}
    url = ("https://ovc.catastro.meh.es/OVCServWeb/OVCWcfCallejero/COVCCallejero.svc/json/"
           f"Consulta_DNPRC?RefCat={cat}")
    try:
        j = json.loads(http_get(url, accept="application/json", timeout=20))
        # La estructura varía; extracción defensiva.
        s = json.dumps(j)
        out = {}
        m = re.search(r'"sfc":\s*"?(\d+)', s)      # superficie construida
        if m: out["m2"] = int(m.group(1))
        m = re.search(r'"ant":\s*"?(\d{4})', s)     # antigüedad
        if m: out["anyo"] = int(m.group(1))
        return out
    except Exception:
        return {}


def valorar(rec):
    """PLACEHOLDER de valoración. TODO: sustituir por comparables reales de venta/alquiler."""
    eurm2 = ZONA_EURM2.get(rec.get("prov"), ZONA_EURM2_DEF)
    if rec.get("muni") == "Barcelona": eurm2 = ZONA_EURM2["Barcelona"]
    if rec.get("m2"):
        rec.setdefault("mercadoEst", round(rec["m2"] * eurm2))
        rec.setdefault("alquilerEst", round(rec["m2"] * eurm2 * 0.004))  # ~0.4%/mes aprox
    else:
        rec.setdefault("mercadoEst", None)
        rec.setdefault("alquilerEst", 0)
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
    hoy = date.today()
    nuevas = 0
    for i in range(DIAS_ATRAS):
        d = hoy - timedelta(days=i)
        root = fetch_sumario(d)
        if root is None: continue
        for it in walk_items(root):
            prov = es_subasta_inmueble_cataluna(it)
            if not prov: continue
            if it["id"] in conocidas:            # ya la teníamos
                continue
            det = parse_documento(it["url_xml"]); time.sleep(0.3)
            det.update(enrich_catastro(det.get("cat")))
            rec = {
                "id": it["id"],
                "org": it["departamento"] or "BOE",
                "tipo": tipo_de(it["titulo"]),
                "addr": (it["titulo"][:90] if it["titulo"] else "—"),
                "muni": prov, "prov": prov,
                "boe_xml": it["url_xml"],
                "first_seen": hoy.isoformat(),
                **det,
            }
            rec = valorar(rec)
            conocidas[rec["id"]] = rec
            nuevas += 1
            print(f"  + {rec['id']}  {rec['tipo']}  {prov}")

    # Descartar concluidas / demasiado antiguas
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

    OUT.write_text(json.dumps(
        {"generated": hoy.isoformat(), "source": "BOE datos abiertos",
         "count": len(vivas), "auctions": list(vivas.values())},
        ensure_ascii=False, indent=1), "utf-8")
    print(f"OK · {nuevas} nuevas · {len(vivas)} activas · escrito {OUT}")


if __name__ == "__main__":
    main()
