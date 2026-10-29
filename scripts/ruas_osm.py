#!/usr/bin/env python3
"""Gera dados/ruas/<código IBGE>.json a partir de um extrato .osm.pbf, para a planta de
localização do CRM Solar não depender mais do Overpass API ao vivo.

As ruas (nome, tipo de via, geometria) ficam publicadas como arquivos estáticos, no mesmo
esquema de dados/<código>.json dos trafos. Uma rua pode aparecer em mais de uma pasta (RGE,
CEEE, cooperativas) quando o município dela está em mais de um meta.json — é intencional, cada
distribuidora fica com uma cópia própria, igual já acontece com trafos/alimentadores.

Uso:
    python3 scripts/ruas_osm.py sul-latest.osm.pbf

Lê o meta.json de cada pasta (DISTS, de scripts/unificar.py) para saber o retângulo de cada
município, filtra as vias (highway) do .pbf que caem em algum retângulo e grava
<pasta>dados/ruas/<código>.json. Tipos de via e sua largura: crm/desenho.py (LARGURAS) — mantenha
a mesma lista aqui.
"""
import json
import sys
from pathlib import Path

import osmium

RAIZ = Path(__file__).resolve().parent.parent

# Mesma lista de scripts/unificar.py (DISTS) — não importamos o módulo porque ele carrega
# trafos_bdgd.py, que exige pandas (pesado demais só para esta lista de pastas).
DISTS = [("RGE", "RGE", "", "4316907"),
         ("CEEE", "CEEE Equatorial", "ceee/", "4314902")]
DISTS += [(k, n, f"rs/{k.lower()}/", None) for k, n in [
    ("Celetro", "Celetro"), ("Cerfox", "Cerfox"), ("Ceriluz", "Ceriluz"), ("Cermissoes", "Cermissões"),
    ("Certaja", "Certaja"), ("Certel", "Certel"), ("Certhil", "Certhil"), ("Cooperluz", "Cooperluz"),
    ("Coopernorte", "Coopernorte"), ("Coopersul", "Coopersul"), ("Coprel", "Coprel"), ("Creluz", "Creluz"),
    ("Creral", "Creral"), ("Demei", "Demei"), ("Eletrocar", "Eletrocar"), ("Hidropan", "Hidropan"),
    ("Mux", "Mux Energia"), ("NovaPalma", "Nova Palma")]]

# Mesma lista de crm/desenho.py (LARGURAS) — vias que entram no desenho da planta.
TIPOS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
         "living_street", "pedestrian", "road", "service", "track",
         "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}

MARGEM = 0.01  # graus (~1,1 km), mesma margem usada na consulta ao Overpass


def carregar_municipios(dists):
    """[(pasta, código, nome, [s, w, n, e])] de todas as distribuidoras que têm meta.json."""
    municipios = []
    for _, nome, pasta, _ in dists:
        caminho = RAIZ / pasta / "dados" / "meta.json"
        if not caminho.exists():
            continue
        meta = json.loads(caminho.read_text(encoding="utf-8"))
        for m in meta.get("muns", []):
            b = m.get("b")
            if not b:
                continue
            municipios.append((pasta, m["c"], m["n"], (b[0] - MARGEM, b[1] - MARGEM,
                                                         b[2] + MARGEM, b[3] + MARGEM)))
    return municipios


CELULA = 0.5  # graus; tamanho da célula do índice espacial dos municípios


def _celulas(s, w, n, e):
    """Células (lat, lon) que o retângulo cobre, para indexar/consultar por grade."""
    cs, ce = int(s // CELULA), int(n // CELULA)
    co, cl = int(w // CELULA), int(e // CELULA)
    return [(y, x) for y in range(cs, ce + 1) for x in range(co, cl + 1)]


class ColetorRuas(osmium.SimpleHandler):
    """Separa as vias por município (bbox) numa só passada pelo arquivo."""

    def __init__(self, municipios):
        super().__init__()
        self.municipios = municipios
        self.indice = {}  # (lat, lon) da célula -> [índice em self.municipios, ...]
        for i, (_, _, _, b) in enumerate(municipios):
            for cel in _celulas(*b):
                self.indice.setdefault(cel, []).append(i)
        # pasta -> código -> lista de ruas
        self.ruas = {}

    def way(self, w):
        tipo = w.tags.get("highway")
        if tipo not in TIPOS:
            return
        nome = w.tags.get("name") or w.tags.get("ref") or ""
        if tipo == "service" and not nome:
            return  # garagem, estacionamento, acesso interno: só polui o desenho
        try:
            pts = [(round(n.lat, 6), round(n.lon, 6)) for n in w.nodes if n.location.valid()]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2:
            return
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        s, n, wo, e = min(lats), max(lats), min(lons), max(lons)
        candidatos = set()
        for cel in _celulas(s, wo, n, e):
            candidatos.update(self.indice.get(cel, ()))
        if not candidatos:
            return
        via = None
        for i in candidatos:
            pasta, codigo, _, (ms, mw, mn, me) = self.municipios[i]
            if n < ms or s > mn or e < mw or wo > me:
                continue  # não intersecta de verdade (a célula só é uma aproximação)
            if via is None:
                via = {"n": nome, "t": tipo, "p": [list(p) for p in pts]}
            self.ruas.setdefault(pasta, {}).setdefault(codigo, []).append(via)


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python3 scripts/ruas_osm.py <extrato.osm.pbf>")
    pbf = sys.argv[1]
    municipios = carregar_municipios(DISTS)
    print(f"{len(municipios)} municípios (todas as distribuidoras); lendo {pbf}...")

    coletor = ColetorRuas(municipios)
    coletor.apply_file(pbf, locations=True, idx="flex_mem")

    total_arquivos = total_ruas = 0
    for pasta, por_codigo in coletor.ruas.items():
        destino = RAIZ / pasta / "dados" / "ruas"
        destino.mkdir(parents=True, exist_ok=True)
        for codigo, ruas in por_codigo.items():
            (destino / f"{codigo}.json").write_text(
                json.dumps(ruas, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            total_arquivos += 1
            total_ruas += len(ruas)
    print(f"Gravados {total_arquivos} arquivos, {total_ruas} trechos de rua no total.")


if __name__ == "__main__":
    main()
