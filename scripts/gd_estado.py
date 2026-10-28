#!/usr/bin/env python3
"""GDs existentes de cada distribuidora, para a aba GDs do site (estado todo).

Local de cada GD = ponto de conexão da BDGD (UGBT/UGMT → PN_CON → PONNOT: poste ou estrutura).
Potência, classe, modalidade e fonte = base de GD da ANEEL pelo CEG_GD (sem casamento: POT_INST em
BT e DEM_CONT em MT, como no trafos_bdgd.py).

Grava <pasta>/dados/gd_estado.json:
{"cl": [classes], "mo": [modalidades], "fo": [fontes],
 "g": [[lat, lon, kW, mt, classe, modalidade, ano de conexão − 2000, fonte (só se não for solar)], ...]}
(maiores primeiro; coordenadas com 4 casas ≈ 10 m, para o arquivo ficar leve)

Uso: python3 -I scripts/gd_estado.py BDGD.zip gd.parquet pasta_dados_do_site
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio

CLASSES = ["Residencial", "Rural", "Comercial", "Industrial", "Poder Público", "Serviço Público", "Outras"]
MODALIDADES = ["Geração na própria UC", "Autoconsumo remoto", "Condomínio", "Compartilhada", "Sem dado"]
FONTES = ["Solar", "Térmica", "Eólica", "Hidráulica", "Outra"]


def txt(s):
    return s.astype(object).where(s.notna(), "").astype(str).str.strip().replace({"None": "", "nan": "", "<NA>": ""})


def abrir_gdb(caminho):
    import zipfile
    with zipfile.ZipFile(caminho) as z:
        g = sorted({n.split(".gdb/")[0] + ".gdb" for n in z.namelist() if ".gdb/" in n})[0]
    return f"/vsizip/{Path(caminho).resolve().as_posix()}/{g}"


def ler(gdb, camada, cols):
    try:
        info = pyogrio.read_info(gdb, layer=camada)
    except Exception:
        return pd.DataFrame(columns=cols)
    tem = {c.upper(): c for c in info["fields"]}
    d = pyogrio.read_dataframe(gdb, layer=camada, read_geometry=False, columns=[tem[c] for c in cols if c in tem])
    d = d.rename(columns={tem[c]: c for c in cols if c in tem})
    for c in cols:
        if c not in d.columns:
            d[c] = None
    return d


def idx_classe(s):
    m = {"Residencial": 0, "Rural": 1, "Comercial": 2, "Industrial": 3, "Poder Público": 4, "Serviço Público": 5}
    return s.map(m).fillna(6).astype(int)


def idx_modalidade(s):
    m = {"Geracao na propria UC": 0, "Auto consumo remoto": 1, "Condomínio": 2, "Compartilhada": 3}
    return s.map(m).fillna(4).astype(int)


def idx_fonte(s):
    m = {"UFV": 0, "UTE": 1, "EOL": 2, "CGH": 3}
    return s.map(m).fillna(4).astype(int)


def main():
    zip_bdgd, base_gd, destino = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    gdb = abrir_gdb(zip_bdgd)
    aneel = pd.read_parquet(base_gd, columns=["CodEmpreendimento", "MdaPotenciaInstaladaKW", "DscClasseConsumo",
                                              "DscModalidadeHabilitado", "SigTipoGeracao"])
    aneel["CodEmpreendimento"] = txt(aneel["CodEmpreendimento"])
    aneel = aneel.drop_duplicates("CodEmpreendimento").set_index("CodEmpreendimento")
    partes = []
    for camada, mt, alt in (("UGBT_tab", 0, "POT_INST"), ("UGMT_tab", 1, "DEM_CONT")):
        d = ler(gdb, camada, ["CEG_GD", "PN_CON", "POT_INST", "DEM_CONT", "MUN", "SIT_ATIV", "DAT_CON"])
        d = d[txt(d["SIT_ATIV"]).str.upper().isin(["AT", ""])]
        ceg = txt(d["CEG_GD"])
        a = aneel.reindex(ceg)
        kw = pd.Series(a["MdaPotenciaInstaladaKW"].to_numpy(), index=d.index) / ceg.map(ceg.value_counts())
        kw = kw.where(ceg != "").fillna(pd.to_numeric(d[alt], errors="coerce"))
        ano = pd.to_datetime(txt(d["DAT_CON"]), errors="coerce", dayfirst=True).dt.year
        partes.append(pd.DataFrame({
            "PN": txt(d["PN_CON"]).to_numpy(), "kw": kw.to_numpy(), "mt": mt,
            "cl": idx_classe(pd.Series(a["DscClasseConsumo"].to_numpy())).to_numpy(),
            "mo": idx_modalidade(pd.Series(a["DscModalidadeHabilitado"].to_numpy())).to_numpy(),
            "fo": idx_fonte(pd.Series(a["SigTipoGeracao"].to_numpy())).to_numpy(),
            "ano": ano.fillna(0).astype(int).to_numpy(), "mun": txt(d["MUN"]).str.lstrip("0").to_numpy()}))
    g = pd.concat(partes, ignore_index=True)
    g = g[g["kw"] > 0]
    pn = pyogrio.read_dataframe(gdb, layer="PONNOT", columns=["COD_ID"])
    if pn.crs is not None and pn.crs.to_epsg() != 4326:
        pn = pn.to_crs(4326)
    pn = pd.DataFrame({"PN": txt(pn["COD_ID"]), "lat": pn.geometry.y.to_numpy(), "lon": pn.geometry.x.to_numpy()})
    pn = pn[pn["PN"].isin(set(g["PN"]))].drop_duplicates("PN")
    g = g.merge(pn, on="PN", how="inner").dropna(subset=["lat", "lon"]).sort_values("kw", ascending=False)
    def kw(v):
        return int(round(v)) if v >= 10 else round(v, 1)
    linhas = [[round(float(r.lat), 4), round(float(r.lon), 4), kw(float(r.kw)), int(r.mt), int(r.cl), int(r.mo),
               max(0, int(r.ano) - 2000)] + ([int(r.fo)] if r.fo else []) for r in g.itertuples(index=False)]
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "gd_estado.json").write_text(json.dumps({"cl": CLASSES, "mo": MODALIDADES, "fo": FONTES, "g": linhas},
                                                       ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{destino / 'gd_estado.json'}: {len(linhas):,} GDs, {g['kw'].sum() / 1000:,.1f} MW "
          f"({np.mean(g['mt'] == 1) * 100:.1f}% em MT)")


if __name__ == "__main__":
    main()
