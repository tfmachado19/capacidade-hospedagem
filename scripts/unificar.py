#!/usr/bin/env python3
"""Monta a página única com várias distribuidoras (filtro "Distribuidora" + lista de municípios).

Lê os dados embutidos (META) das páginas de cada distribuidora, junta e gera a página com o
código do TEMPLATE de trafos_bdgd.py. Os dados de cada distribuidora continuam na pasta dela.

Uso: python3 scripts/unificar.py [saida]   (padrão: beta/index.html)
"""
import importlib.util, json, re, sys, unicodedata
from pathlib import Path

raiz = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tb", raiz / "scripts" / "trafos_bdgd.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

# (chave, nome, pasta dos dados, município inicial). A lista de municípios de cada uma vem de
# <pasta>dados/meta.json, gravado pelo trafos_bdgd.py (--mun todos) junto com os dados.
DISTS = [("RGE", "RGE", "", "4316907"),
         ("CEEE", "CEEE Equatorial", "ceee/", "4314902")]
# Cooperativas e permissionárias do RS (dados em rs/<pasta>/dados/; abrem no município padrão do meta.json)
DISTS += [(k, n, f"rs/{k.lower()}/", None) for k, n in [
    ("Celetro", "Celetro"), ("Cerfox", "Cerfox"), ("Ceriluz", "Ceriluz"), ("Cermissoes", "Cermissões"),
    ("Certaja", "Certaja"), ("Certel", "Certel"), ("Certhil", "Certhil"), ("Cooperluz", "Cooperluz"),
    ("Coopernorte", "Coopernorte"), ("Coopersul", "Coopersul"), ("Coprel", "Coprel"), ("Creluz", "Creluz"),
    ("Creral", "Creral"), ("Demei", "Demei"), ("Eletrocar", "Eletrocar"), ("Hidropan", "Hidropan"),
    ("Mux", "Mux Energia"), ("NovaPalma", "Nova Palma")]]


def meta_de(pasta):
    return json.loads((raiz / pasta / "dados" / "meta.json").read_text(encoding="utf-8"))


def main():
    saida = raiz / (sys.argv[1] if len(sys.argv) > 1 else "beta/index.html")
    metas, muns, dists = [], [], []
    for k, nome, pasta, padrao in DISTS:
        if not (raiz / pasta / "dados" / "meta.json").exists():
            print(f"  (sem dados de {nome} em {pasta}dados/, fica de fora)")
            continue
        m = meta_de(pasta)
        padrao = padrao or m["default"]
        metas.append(m)
        muns += [dict(x, d=k, k=f"{k}:{x['c']}") for x in m["muns"]]
        d = {"k": k, "n": nome, "p": pasta, "padrao": f"{k}:{padrao}"}
        if m.get("fgd") is not None and m["fgd"] != 1:
            d["g"] = m["fgd"]   # fração da GD descontada da carga mínima (calibrada com os orçamentos)
        if (raiz / pasta / "dados" / "orc.json").exists():
            d["o"] = 1      # tem estudos da distribuidora (scripts/orcamentos.py)
        dists.append(d)
    chave = lambda x: (unicodedata.normalize("NFKD", x["n"]).encode("ascii", "ignore").decode().lower(), x["d"])
    muns.sort(key=chave)
    b = [m["bboxAll"] for m in metas]
    meta = dict(metas[0])
    meta.update(muns=muns, dists=dists, default=dists[0]["padrao"], mun=None, bbox=None,
                bboxAll=[min(x[0] for x in b), min(x[1] for x in b), max(x[2] for x in b), max(x[3] for x in b)],
                titulo=" e ".join(d["n"] for d in dists) if len(dists) <= 3 else "distribuidoras do RS",
                distTexto=" e da ".join(d["n"] for d in dists) if len(dists) <= 3 else "distribuidora")
    saida.write_text(tb._html("null", meta, meta["titulo"]), encoding="utf-8")
    print(f"{saida}: {len(muns)} municípios de {len(dists)} distribuidoras")


if __name__ == "__main__":
    main()
