#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prospeccao.py — Planilha de prospecção comercial de GD a partir dos dados abertos da ANEEL.

ENTRADAS
  - BDGD da distribuidora (.zip com .gdb)                    → UCs, GDs, bairros
  - UCBT_PJ e UCMT_PJ (dados abertos ANEEL, "BDGD - PJ")      → empresas com endereço,
    CNAE, consumo e coordenada (o CNPJ vem criptografado)
      https://dadosabertos.aneel.gov.br/dataset/base-de-dados-geografica-da-distribuidora-bdgd
  - BDGD_trafos_<...>.xlsx gerado pelo trafos_bdgd.py        → capacidade do trafo e do alimentador

USO
  py scripts/prospeccao.py --bdgd RGE.zip --ucbt-pj ucbt_pj.zip --ucmt-pj ucmt_pj.csv \
       --trafos BDGD_trafos_todos.xlsx [--dist 396] [--mun 4316907] [--min-kwh 300] [--saida pasta]

SAÍDA: Prospeccao_<mun|todos>.xlsx com as abas Empresas BT, Empresas MT, Bairros,
Atividades, Municípios e Leia-me.

ATENÇÃO: o consumo de quem já tem GD é o líquido medido pela distribuidora. A coluna "GD"
indica GD cadastrada na própria UC (CEG_GD) ou no mesmo ponto de conexão (PAC), que pode
ser de um vizinho. Estimativa para triagem comercial.
"""
import argparse
import json
import io
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyogrio

ENE = [f"ENE_{i:02d}" for i in range(1, 13)]
DIC = [f"DIC_{i:02d}" for i in range(1, 13)]
FIC = [f"FIC_{i:02d}" for i in range(1, 13)]
DEM = [f"DEM_{i:02d}" for i in range(1, 13)]
KWH_KWP = 1150          # geração FV anual típica no RS (kWh por kWp)
# Pg para afastamento da análise de inversão de fluxo (REN 1.000 art. 73-A, II; Manual item 8.12):
# Pg = C / (FC × 24 × 30) × FA, FC = 16% (art. 655-B), FA do Anexo II por classe, arredondado para cima
FC_SOLAR = 0.16
FA_CLASSE = {"RE": 0.46, "IN": 0.69, "CO": 0.63, "RU": 0.53, "SP": 0.57, "PP": 0.57, "IP": 0.57,
             "CP": 0.63, "CS": 0.63}
T0 = time.time()

CLASSE = {"RE": "Residencial", "CO": "Comercial", "IN": "Industrial", "RU": "Rural",
          "PP": "Poder público", "SP": "Serviço público", "IP": "Iluminação pública",
          "CP": "Consumo próprio", "CS": "Consumo próprio"}

# CNAE 2.0 — divisões (2 primeiros dígitos)
CNAE_DIV = {
    "01": "Agricultura, pecuária e serviços relacionados", "02": "Produção florestal",
    "03": "Pesca e aquicultura", "05": "Extração de carvão mineral", "06": "Extração de petróleo e gás",
    "07": "Extração de minerais metálicos", "08": "Extração de minerais não metálicos",
    "09": "Apoio à extração de minerais", "10": "Fabricação de produtos alimentícios",
    "11": "Fabricação de bebidas", "12": "Fabricação de produtos do fumo", "13": "Fabricação de produtos têxteis",
    "14": "Confecção de vestuário e acessórios", "15": "Couros, artefatos de couro e calçados",
    "16": "Fabricação de produtos de madeira", "17": "Celulose, papel e produtos de papel",
    "18": "Impressão e reprodução de gravações", "19": "Coque, derivados de petróleo e biocombustíveis",
    "20": "Fabricação de produtos químicos", "21": "Produtos farmoquímicos e farmacêuticos",
    "22": "Produtos de borracha e plástico", "23": "Produtos de minerais não metálicos", "24": "Metalurgia",
    "25": "Produtos de metal (exceto máquinas)", "26": "Informática, eletrônicos e ópticos",
    "27": "Máquinas, aparelhos e materiais elétricos", "28": "Fabricação de máquinas e equipamentos",
    "29": "Veículos automotores, reboques e carrocerias", "30": "Outros equipamentos de transporte",
    "31": "Fabricação de móveis", "32": "Fabricação de produtos diversos",
    "33": "Manutenção e instalação de máquinas", "35": "Eletricidade, gás e outras utilidades",
    "36": "Captação, tratamento e distribuição de água", "37": "Esgoto",
    "38": "Coleta e tratamento de resíduos; reciclagem", "39": "Descontaminação e gestão de resíduos",
    "41": "Construção de edifícios", "42": "Obras de infraestrutura", "43": "Serviços especializados para construção",
    "45": "Comércio e reparação de veículos", "46": "Comércio atacadista", "47": "Comércio varejista",
    "49": "Transporte terrestre", "50": "Transporte aquaviário", "51": "Transporte aéreo",
    "52": "Armazenamento e apoio aos transportes", "53": "Correio e entregas", "55": "Alojamento",
    "56": "Alimentação (restaurantes, bares)", "58": "Edição", "59": "Cinema, vídeo, TV e música",
    "60": "Rádio e televisão", "61": "Telecomunicações", "62": "Tecnologia da informação",
    "63": "Serviços de informação", "64": "Serviços financeiros", "65": "Seguros e previdência",
    "66": "Auxiliares de serviços financeiros", "68": "Atividades imobiliárias",
    "69": "Jurídico, contabilidade e auditoria", "70": "Sedes de empresas e consultoria em gestão",
    "71": "Arquitetura, engenharia e análises técnicas", "72": "Pesquisa e desenvolvimento",
    "73": "Publicidade e pesquisa de mercado", "74": "Outras atividades profissionais e técnicas",
    "75": "Atividades veterinárias", "77": "Aluguéis não imobiliários", "78": "Seleção e locação de mão de obra",
    "79": "Agências de viagens e turismo", "80": "Vigilância e segurança", "81": "Serviços para edifícios e paisagismo",
    "82": "Serviços de escritório e apoio administrativo", "84": "Administração pública",
    "85": "Educação", "86": "Saúde humana", "87": "Saúde e assistência social em residências coletivas",
    "88": "Assistência social sem alojamento", "90": "Atividades artísticas e de espetáculos",
    "91": "Patrimônio cultural e ambiental", "92": "Jogos de azar e apostas", "93": "Esporte, recreação e lazer",
    "94": "Organizações associativas (igrejas, sindicatos, associações)",
    "95": "Reparação de equipamentos e objetos pessoais", "96": "Outros serviços pessoais",
    "97": "Serviços domésticos", "99": "Organismos internacionais",
}


def log(m):
    print(f"[{time.time() - T0:6.1f}s] {m}", flush=True)


def txt(s):
    return s.astype(object).where(s.notna(), "").astype(str).str.strip().replace({"None": "", "nan": "", "<NA>": ""})


def num(df, cols):
    return df.reindex(columns=cols).apply(pd.to_numeric, errors="coerce").fillna(0)


def abrir_gdb(caminho):
    p = Path(caminho)
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            g = sorted({n.split(".gdb/")[0] + ".gdb" for n in z.namelist() if ".gdb/" in n})[0]
        return f"/vsizip/{p.resolve().as_posix()}/{g}"
    return str(p)


def ler_pj(caminho, dist, mun):
    """Lê UCBT_PJ/UCMT_PJ (.csv, .zip com .csv ou .parquet já filtrado), só da distribuidora."""
    p = Path(caminho)
    if p.suffix.lower() == ".parquet":
        d = pd.read_parquet(p)
    else:
        if p.suffix.lower() == ".zip":
            z = zipfile.ZipFile(p)
            f = io.TextIOWrapper(z.open([n for n in z.namelist() if n.lower().endswith(".csv")][0]),
                                 encoding="latin-1")
        else:
            f = open(p, encoding="latin-1")
        partes = [c[c["DIST"].astype(str) == str(dist)]
                  for c in pd.read_csv(f, sep=";", dtype=str, chunksize=500_000)]
        d = pd.concat(partes, ignore_index=True)
    d = d[d["DIST"].astype(str) == str(dist)]
    d = d[txt(d["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    if mun:
        d = d[txt(d["MUN"]) == str(mun)]
    return d.reset_index(drop=True)


def ler_camada(gdb, camada, cols, filtro=None):
    """Lê só as colunas que existem na camada (BDGDs de distribuidoras diferentes variam)."""
    try:
        info = pyogrio.read_info(gdb, layer=camada)
    except Exception:
        return pd.DataFrame(columns=cols)
    tem = {c.upper(): c for c in info["fields"]}
    usar = [tem[c] for c in cols if c in tem]
    d = pyogrio.read_dataframe(gdb, layer=camada, read_geometry=False, columns=usar, where=filtro)
    d = d.rename(columns={tem[c]: c for c in cols if c in tem})
    for c in cols:
        if c not in d.columns:
            d[c] = None
    return d


def gd_com_local(gdb, ug, ugmt, base_gd):
    """GDs de BT e MT com potência (base da ANEEL pelo CEG_GD; sem casamento: POT_INST em BT, DEM_CONT em MT)
    e o local do ponto de conexão (PN_CON → PONNOT: poste ou estrutura)."""
    kwmap = None
    if base_gd:
        g = pd.read_parquet(base_gd, columns=["CodEmpreendimento", "MdaPotenciaInstaladaKW"])
        g["CodEmpreendimento"] = txt(g["CodEmpreendimento"])
        g["kw"] = pd.to_numeric(g["MdaPotenciaInstaladaKW"].astype(str).str.replace(",", "."), errors="coerce")
        kwmap = g.dropna().drop_duplicates("CodEmpreendimento").set_index("CodEmpreendimento")["kw"]
    partes = []
    for d, mt, alt in ((ug, 0, "POT_INST"), (ugmt, 1, "DEM_CONT")):
        ceg = txt(d["CEG_GD"])
        kw = ceg.map(kwmap) / ceg.map(ceg.value_counts()) if kwmap is not None else pd.Series(np.nan, index=d.index)
        kw = kw.fillna(pd.to_numeric(d[alt], errors="coerce"))
        partes.append(pd.DataFrame({"PAC": txt(d["PAC"]), "PN": txt(d["PN_CON"]), "CEG": ceg,
                                    "kw": kw.fillna(0).to_numpy(), "mt": mt}))
    gds = pd.concat(partes, ignore_index=True)
    pn = pyogrio.read_dataframe(gdb, layer="PONNOT", columns=["COD_ID"])
    if pn.crs is not None and pn.crs.to_epsg() != 4326:
        pn = pn.to_crs(4326)
    pn = pd.DataFrame({"PN": txt(pn["COD_ID"]), "lat": pn.geometry.y.to_numpy(), "lon": pn.geometry.x.to_numpy()})
    pn = pn[pn["PN"].isin(set(gds["PN"]))].drop_duplicates("PN")
    return gds.merge(pn, on="PN", how="left")


from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE as ILEGAL  # noqa: E402

RAIO_MT = 150   # metros: GD em MT perto da empresa


def gd_da_empresa(d, gds, uc_pac):
    """(texto, código, lista) por empresa. Código: 3 = tem GD (certo), 1 = há GD por perto não confirmada,
    0 = sem GD. Lista: [[lat, lon, kW, mt, relação, distância m, nº de GDs]], relação 0 = da empresa,
    1 = mesmo ponto de conexão com outras unidades, 2 = mesmo poste, 3 = em MT perto."""
    pac, pn = txt(d["PAC"]).to_numpy(), txt(d["PN_CON"]).to_numpy() if "PN_CON" in d.columns else np.full(len(d), "")
    ceg = txt(d["CEG_GD"]).to_numpy() if "CEG_GD" in d.columns else np.full(len(d), "")
    lat = pd.to_numeric(d["POINT_Y"], errors="coerce").to_numpy()
    lon = pd.to_numeric(d["POINT_X"], errors="coerce").to_numpy()
    ok = gds.dropna(subset=["lat", "lon"])
    agrega = lambda g: [round(float(g["lat"].iat[0]), 6), round(float(g["lon"].iat[0]), 6),
                        round(float(g["kw"].sum()), 1), int(g["mt"].iat[0]), int(len(g))]
    por_pac = {k: g for k, g in ok.groupby("PAC") if k}
    por_pn = {k: g for k, g in ok.groupby("PN") if k}
    por_ceg = {k: g for k, g in ok.groupby("CEG") if k}
    mt = ok[ok["mt"] == 1]
    grade = {}
    for i, (la, lo) in enumerate(zip(mt["lat"].to_numpy(), mt["lon"].to_numpy())):
        grade.setdefault((int(la // 0.005), int(lo // 0.005)), []).append(i)
    mla, mlo = mt["lat"].to_numpy(), mt["lon"].to_numpy()

    def dist(la1, lo1, la2, lo2):
        return float(np.hypot((la2 - la1) * 110540, (lo2 - lo1) * 111320 * np.cos(np.radians(la1))))

    textos, codigos, listas = [], [], []
    for k in range(len(d)):
        itens, vistos = [], set()

        def junta(g, rel):
            novos = g[~g.index.isin(vistos)]
            if not len(novos):
                return
            vistos.update(novos.index)
            for _, gg in novos.groupby("PN"):
                la, lo, kw, m, n = agrega(gg)
                itens.append([la, lo, kw, m, rel, round(dist(lat[k], lon[k], la, lo)) if np.isfinite(lat[k]) else None, n])

        certo = False
        if ceg[k] and ceg[k] in por_ceg:
            junta(por_ceg[ceg[k]], 0)
            certo = True
        elif ceg[k]:
            certo = True                          # CEG na UC, mas a GD não tem local na BDGD
        if pac[k] in por_pac:
            so_ela = uc_pac.get(pac[k], 1) <= 1
            junta(por_pac[pac[k]], 0 if so_ela else 1)
            certo = certo or so_ela
        if pn[k] in por_pn:
            junta(por_pn[pn[k]], 2)
        if np.isfinite(lat[k]) and len(mt):
            ci, cj = int(lat[k] // 0.005), int(lon[k] // 0.005)
            perto = [i for a in (-1, 0, 1) for b in (-1, 0, 1) for i in grade.get((ci + a, cj + b), [])
                     if dist(lat[k], lon[k], mla[i], mlo[i]) <= RAIO_MT]
            if perto:
                junta(mt.iloc[perto], 3)
        itens.sort(key=lambda x: (x[4], x[5] if x[5] is not None else 0))
        itens = itens[:8]
        if certo:
            kw = sum(x[2] for x in itens if x[4] == 0)
            textos.append("sim (GD cadastrada na UC)" if ceg[k] else
                          "sim (GD no ponto de conexão, que só atende esta unidade)")
            codigos.append(3)
            if kw:
                textos[-1] += f" · {kw:,.1f} kW"
        elif itens:
            rel = {1: "no mesmo ponto de conexão, com outras unidades", 2: "no mesmo poste",
                   3: f"em MT a até {RAIO_MT} m"}
            textos.append("não confirmada: GD " + "; ".join(
                f"{rel[x[4]]} ({x[2]:,.1f} kW{f', {x[5]} m' if x[4] == 3 and x[5] is not None else ''})" for x in itens[:3]))
            codigos.append(1)
        else:
            textos.append("não")
            codigos.append(0)
        listas.append(itens)
    return textos, codigos, listas


def salvar_site_emp(emp, site):
    """dados/emp/<código IBGE>.json: {"a": {divisão: nome}, "e": [[lat, lon, kWh/mês, kWp, divisão, classe,
    gd, rede, endereço, bairro, grupo, mt, DIC, trafo, alimentador, demanda contratada, CNAE, Pg, GDs], ...]}
    gd: 0 sem GD, 1 GD por perto não confirmada, 3 tem GD (certo) · rede: 0 sem dado, 1 não, 2 parcial, 3 sim
    GDs: [[lat, lon, kW, mt, relação, distância m, nº]] (ver gd_da_empresa); vazio quando não há"""
    pasta = Path(site) / "dados" / "emp"
    pasta.mkdir(parents=True, exist_ok=True)
    emp = emp.dropna(subset=["Latitude", "Longitude"])
    gd = emp["_gd"].fillna(0).astype(int)
    rede = emp["Rede comporta o sistema?"].str.split(" ").str[0].map(
        {"sem": 0, "não": 1, "parcial": 2, "sim": 3}).fillna(0).astype(int)
    r1 = lambda v, d=1: None if pd.isna(v) else round(float(v), d)
    linhas = pd.DataFrame({
        "mun": emp["Município (IBGE)"], "lat": emp["Latitude"].round(6), "lon": emp["Longitude"].round(6),
        "kwh": emp["Consumo médio 12 meses (kWh)"].round(0), "pg": emp["Pg sem análise de inversão (kW)"], "kwp": emp["Sistema FV sugerido (kWp)"].round(1),
        "div": emp["CNAE"].str[:2], "cls": emp["Classe"], "gd": gd, "rede": rede, "end": emp["Endereço"],
        "brr": emp["Bairro"], "gru": emp["Grupo tarifário"], "mt": emp["_mt"],
        "dic": emp["Horas sem energia no ano (DIC)"].round(1),
        "tr": emp.get("Trafo", pd.Series("", index=emp.index)).fillna(""), "al": emp["Alimentador"].fillna(""),
        "dem": emp.get("Demanda contratada (kW)", pd.Series(np.nan, index=emp.index)), "cnae": emp["CNAE"],
        "gdl": emp["_gdl"]})
    n = 0
    for mun, g in linhas.groupby("mun"):
        divs = sorted(set(g["div"]) - {""})
        e = [[r.lat, r.lon, int(r.kwh), r.kwp, r.div, r.cls, int(r.gd), int(r.rede), r.end, r.brr, r.gru,
              int(r.mt), r1(r.dic), r.tr, r.al, r1(r.dem, 0), r.cnae, int(r.pg), r.gdl or []]
             for r in g.itertuples(index=False)]
        d = {"a": {k: CNAE_DIV.get(k, k) for k in divs}, "e": e}
        (pasta / f"{mun}.json").write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="Prospecção comercial de GD (dados abertos ANEEL)")
    ap.add_argument("--bdgd", required=True)
    ap.add_argument("--ucbt-pj", required=True)
    ap.add_argument("--ucmt-pj", required=True)
    ap.add_argument("--trafos", required=True, help="BDGD_trafos_*.xlsx do trafos_bdgd.py")
    ap.add_argument("--dist", default="396", help="código da distribuidora (RGE = 396)")
    ap.add_argument("--mun", default=None, help="código IBGE para filtrar um município")
    ap.add_argument("--min-kwh", type=float, default=300, help="consumo médio mensal mínimo das empresas BT")
    ap.add_argument("--nomes", default=None, help="base de GD da ANEEL (.parquet) p/ nomes dos municípios")
    ap.add_argument("--saida", default=".")
    ap.add_argument("--site", default=None,
                    help="pasta do site (site_todos/) para gravar dados/emp/<município>.json da camada de prospecção")
    a = ap.parse_args()
    gdb = abrir_gdb(a.bdgd)
    filtro = f"MUN = '{a.mun}'" if a.mun else None

    # nomes dos municípios
    nomes = {}
    if a.nomes:
        n = pd.read_parquet(a.nomes, columns=["CodMunicipioIbge", "NomMunicipio"]).drop_duplicates("CodMunicipioIbge")
        nomes = {str(int(c)): v for c, v in zip(n["CodMunicipioIbge"], n["NomMunicipio"])}
    nome_mun = lambda s: txt(s).map(lambda c: nomes.get(c, c))

    # capacidade do trafo e do alimentador (resultado do trafos_bdgd.py)
    log("Lendo capacidade dos trafos ...")
    cols_tr = ["Transformador (COD_ID)", "Potência nominal (kVA)", "Capacidade disponível estimada (kW)",
               "Capacidade disponível p/ GD no alimentador (kW)"]
    tr = pd.read_excel(a.trafos, sheet_name="Transformadores", usecols=cols_tr)
    tr["Transformador (COD_ID)"] = tr["Transformador (COD_ID)"].astype(str)
    tr = tr.drop_duplicates("Transformador (COD_ID)").set_index("Transformador (COD_ID)")
    al = pd.read_excel(a.trafos, sheet_name="Alimentadores",
                       usecols=["Alimentador (CTMT)", "Capacidade disponível p/ GD no alimentador (kW)"])
    cap_alim = al.set_index(al["Alimentador (CTMT)"].astype(str))["Capacidade disponível p/ GD no alimentador (kW)"]

    # GD por ponto de conexão (PAC) e UCs por PAC (BDGD)
    log("Lendo UCs e GDs da BDGD ...")
    ug = ler_camada(gdb, "UGBT_tab", ["PAC", "SIT_ATIV", "MUN", "UNI_TR_MT", "PN_CON", "CEG_GD", "POT_INST"], filtro)
    ug = ug[txt(ug["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    ugmt = ler_camada(gdb, "UGMT_tab", ["PAC", "SIT_ATIV", "MUN", "PN_CON", "CEG_GD", "POT_INST", "DEM_CONT"], filtro)
    ugmt = ugmt[txt(ugmt["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    uc = pyogrio.read_dataframe(gdb, layer="UCBT_tab", read_geometry=False, where=filtro,
                                columns=["PAC", "MUN", "BRR", "CLAS_SUB", "UNI_TR_MT", "SIT_ATIV"] + ENE)
    uc = uc[txt(uc["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    ucmt_pac = ler_camada(gdb, "UCMT_tab", ["PAC", "SIT_ATIV"], filtro)
    ucmt_pac = ucmt_pac[txt(ucmt_pac["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    # nº de unidades consumidoras (BT + MT) em cada ponto de conexão
    uc_pac = pd.concat([txt(uc["PAC"]), txt(ucmt_pac["PAC"])]).value_counts()
    gds = gd_com_local(gdb, ug, ugmt, a.nomes)
    log(f"  {len(gds):,} GDs com local ({int(gds['mt'].sum()):,} em MT)")

    def info_gd(d):
        """GD da empresa. Só é 'sim' com certeza: GD cadastrada na própria UC (CEG_GD) ou no ponto de
        conexão que só tem esta unidade. GD no mesmo ponto com outras unidades, no mesmo poste ou em MT
        a até 300 m fica como 'não confirmada' e vai na lista (desenhada no mapa ao abrir a empresa)."""
        return gd_da_empresa(d, gds, uc_pac)

    def empresas(d, mt):
        E = num(d, ENE)
        anual = E.sum(axis=1)
        fa = txt(d["CLAS_SUB"]).str[:2].map(FA_CLASSE).fillna(0.63)
        out = pd.DataFrame({
            "Município (IBGE)": txt(d["MUN"]),
            "Município": nome_mun(d["MUN"]),
            "Endereço": txt(d["LGRD"]),
            "Bairro": txt(d["BRR"]),
            "CEP": txt(d["CEP"]),
            "Classe": txt(d["CLAS_SUB"]).str[:2].map(CLASSE).fillna(txt(d["CLAS_SUB"])),
            "Subclasse": txt(d["CLAS_SUB"]),
            "CNAE": txt(d["CNAE"]),
            "Atividade (divisão CNAE)": txt(d["CNAE"]).str[:2].map(CNAE_DIV).fillna(""),
            "Grupo tarifário": txt(d["GRU_TAR"]),
            "Fases": txt(d["FAS_CON"]),
            "Consumo médio 12 meses (kWh)": anual / 12,
            "FA (Anexo II, %)": (fa * 100).round(0),
            "Pg sem análise de inversão (kW)": np.ceil(anual / 12 / (FC_SOLAR * 24 * 30) * fa - 1e-9).clip(lower=0),
            "Consumo anual (kWh)": anual,
            "Sistema FV sugerido (kWp)": (anual / KWH_KWP).clip(upper=3000 if mt else 75),
            "GD": "",
            "Cliente desde": txt(d["DAT_CON"]),
            "Horas sem energia no ano (DIC)": num(d, DIC).sum(axis=1),
            "Interrupções no ano (FIC)": num(d, FIC).sum(axis=1),
        })
        if mt:
            dem = pd.to_numeric(d["DEM_CONT"], errors="coerce")
            out.insert(out.columns.get_loc("Consumo médio 12 meses (kWh)"), "Demanda contratada (kW)", dem)
            out.insert(out.columns.get_loc("Consumo médio 12 meses (kWh)"), "Demanda máx. medida (kW)",
                       num(d, DEM).max(axis=1))
            out["Alimentador"] = txt(d["CTMT"])
        else:
            t = txt(d["UNI_TR_MT"])
            out["Trafo"] = t
            out["kVA do trafo"] = t.map(tr["Potência nominal (kVA)"])
            out["Capacidade do trafo p/ GD (kW)"] = t.map(tr["Capacidade disponível estimada (kW)"])
            out["Alimentador"] = txt(d["CTMT"])
        out["Capacidade do alimentador p/ GD (kW)"] = out["Alimentador"].map(cap_alim)
        lim = out["Capacidade do alimentador p/ GD (kW)"]
        if not mt:
            lim = np.fmin(lim, out["Capacidade do trafo p/ GD (kW)"])
        kwp = out["Sistema FV sugerido (kWp)"]
        out["Rede comporta o sistema?"] = np.select(
            [lim.isna(), lim >= kwp, lim > 0], ["sem dado", "sim", "parcial"], "não (precisa de estudo)")
        out["Latitude"] = pd.to_numeric(d["POINT_Y"], errors="coerce")
        out["Longitude"] = pd.to_numeric(d["POINT_X"], errors="coerce")
        texto, codigo, lista = info_gd(d)
        out["GD"], out["_gd"], out["_gdl"] = texto, codigo, lista
        out["Google Maps"] = [f"https://www.google.com/maps?q={la:.6f},{lo:.6f}" if pd.notna(la) else ""
                              for la, lo in zip(out["Latitude"], out["Longitude"])]
        return out.sort_values("Consumo anual (kWh)", ascending=False)

    log("Lendo empresas (UCBT_PJ / UCMT_PJ) ...")
    bt = ler_pj(a.ucbt_pj, a.dist, a.mun)
    mt = ler_pj(a.ucmt_pj, a.dist, a.mun)
    log(f"  {len(bt):,} UCs PJ em BT e {len(mt):,} em MT")
    e_bt_todas = empresas(bt, False)
    e_mt = empresas(mt, True)
    e_bt = e_bt_todas[e_bt_todas["Consumo médio 12 meses (kWh)"] >= a.min_kwh]

    # Bairros (todas as UCs de BT, PF + PJ)
    log("Agregando bairros e municípios ...")
    uc["kwh"] = num(uc, ENE).sum(axis=1)
    uc["res"] = txt(uc["CLAS_SUB"]).str[:2] == "RE"
    uc["MUN"], uc["BRR"], uc["TR"] = txt(uc["MUN"]), txt(uc["BRR"]).str.upper(), txt(uc["UNI_TR_MT"])
    ug["TR"] = txt(ug["UNI_TR_MT"])
    # bairro de cada GD = bairro mais comum das UCs do mesmo trafo
    brr_tr = uc.groupby("TR")["BRR"].agg(lambda s: s.mode().iat[0] if len(s.mode()) else "")
    ug["BRR"], ug["MUN"] = ug["TR"].map(brr_tr).fillna(""), txt(ug["MUN"])
    n_ug = ug.groupby(["MUN", "BRR"]).size()
    cap_tr = uc[["TR", "MUN", "BRR"]].drop_duplicates("TR")
    cap_tr["cap"] = cap_tr["TR"].map(tr["Capacidade disponível estimada (kW)"])
    bai = uc.groupby(["MUN", "BRR"]).agg(**{
        "UCs de BT": ("kwh", "size"), "UCs residenciais": ("res", "sum"),
        "Consumo anual (MWh)": ("kwh", lambda s: s.sum() / 1000),
        "Consumo médio residencial (kWh/mês)": ("kwh", lambda s: np.nan)}).reset_index()
    res_med = uc[uc["res"]].groupby(["MUN", "BRR"])["kwh"].mean() / 12
    bai["Consumo médio residencial (kWh/mês)"] = bai.set_index(["MUN", "BRR"]).index.map(res_med)
    bai["GDs em BT"] = bai.set_index(["MUN", "BRR"]).index.map(n_ug).fillna(0).astype(int)
    bai["Penetração de GD (%)"] = bai["GDs em BT"] / bai["UCs de BT"] * 100
    bai["Potencial FV restante (kWp)"] = bai["Consumo anual (MWh)"] * 1000 * \
        (1 - (bai["Penetração de GD (%)"] / 100).clip(upper=1)) / KWH_KWP
    ct = cap_tr.groupby(["MUN", "BRR"])["cap"].agg(
        **{"Trafos": "size", "Trafos com capacidade ≥ 10 kW": lambda s: (s >= 10).sum(),
           "Capacidade somada dos trafos (kW)": "sum"})
    bai = bai.merge(ct.reset_index(), on=["MUN", "BRR"], how="left")
    bai.insert(1, "Município", nome_mun(bai["MUN"]))
    bai = bai.rename(columns={"MUN": "Município (IBGE)", "BRR": "Bairro"}) \
        .sort_values("Potencial FV restante (kWp)", ascending=False)

    # Atividades
    emp = pd.concat([e_bt_todas.assign(Rede="BT"), e_mt.assign(Rede="MT")], ignore_index=True)
    emp["sem_gd"] = emp["_gd"] < 3          # GD só conta quando é certa
    atv = emp.groupby(emp["CNAE"].str[:2]).agg(**{
        "Empresas": ("Endereço", "size"), "Sem GD": ("sem_gd", "sum"),
        "Consumo anual total (MWh)": ("Consumo anual (kWh)", lambda s: s.sum() / 1000),
        "Consumo médio 12 meses (kWh)": ("Consumo médio 12 meses (kWh)", "mean"),
        "Potencial FV das sem GD (kWp)": ("Sistema FV sugerido (kWp)", lambda s: s[emp.loc[s.index, "sem_gd"]].sum()),
    }).reset_index().rename(columns={"CNAE": "Divisão CNAE"})
    atv.insert(1, "Atividade", atv["Divisão CNAE"].map(CNAE_DIV).fillna(""))
    atv = atv.sort_values("Potencial FV das sem GD (kWp)", ascending=False)

    # Municípios
    mun = bai.groupby(["Município (IBGE)", "Município"]).agg(**{
        "UCs de BT": ("UCs de BT", "sum"), "GDs em BT": ("GDs em BT", "sum"),
        "Consumo anual (MWh)": ("Consumo anual (MWh)", "sum"),
        "Potencial FV restante (kWp)": ("Potencial FV restante (kWp)", "sum"),
        "Trafos com capacidade ≥ 10 kW": ("Trafos com capacidade ≥ 10 kW", "sum")}).reset_index()
    mun["Penetração de GD (%)"] = mun["GDs em BT"] / mun["UCs de BT"] * 100
    eg = emp[emp["sem_gd"] & (emp["Consumo médio 12 meses (kWh)"] >= 1000)].groupby("Município (IBGE)").size()
    mun["Empresas sem GD ≥ 1.000 kWh/mês"] = mun["Município (IBGE)"].map(eg).fillna(0).astype(int)
    mun = mun.sort_values("Potencial FV restante (kWp)", ascending=False)

    leiame = pd.DataFrame({"Como ler": [
        "FONTE: BDGD da distribuidora e bases UCBT_PJ/UCMT_PJ da ANEEL (data-base 31/12/2025). O CNPJ vem criptografado pela ANEEL; não há nome nem CNPJ.",
        "EMPRESAS: unidades consumidoras de pessoa jurídica ativas, com endereço, CNAE, consumo e coordenada informados pela distribuidora. Em BT, só as com consumo médio ≥ " + f"{a.min_kwh:g} kWh/mês.",
        f"GD: 'sim' só quando é certo — GD cadastrada na própria UC (CEG_GD) ou no ponto de conexão que só atende esta unidade. 'não confirmada' = há GD no mesmo ponto de conexão com outras unidades, no mesmo poste ou em MT a até {RAIO_MT} m (pode ser de um vizinho; no site, ao abrir a empresa, essas GDs aparecem no mapa). 'não' = nenhuma GD nesses lugares.",
        "CONSUMO MÉDIO 12 MESES: média de ENE_01..12 da BDGD (jan–dez/2025). Para quem já tem GD é o consumo líquido (o autoconsumo não aparece).",
        "Pg SEM ANÁLISE DE INVERSÃO (kW) = consumo médio 12 meses ÷ (16% × 24 h × 30 dias) × FA, arredondado para cima (Manual de Inversão de Fluxo, itens 8.12–8.15; REN 1.000 arts. 73-A, II e 655-B). FA do Anexo II: residencial 46%, industrial 69%, comercial/serviços 63%, rural 53%, serviço e poder público 57%. A hipótese de afastamento vale para microgeração (até 75 kW) dentro da potência disponibilizada; acima disso o Pg é só referência. Na fatura, o C é o consumo faturado dos 12 últimos ciclos, que pode diferir da BDGD.",
        f"SISTEMA FV SUGERIDO (kWp) = consumo anual ÷ {KWH_KWP} kWh/kWp (geração típica no RS), limitado a 75 kW na BT (microgeração) e 3.000 kW na MT (minigeração solar, Lei 14.300). Não considera custo de disponibilidade.",
        "REDE COMPORTA? compara o sistema sugerido com a capacidade estimada do trafo e do alimentador (BT) ou só do alimentador (MT), do trafos_bdgd.py. 'não' indica que provavelmente precisará de estudo de inversão de fluxo.",
        "DIC/FIC: horas e número de interrupções no ano — argumento para baterias / backup.",
        "MT: limite de minigeração costuma ser a demanda contratada; confira a coluna 'Demanda contratada (kW)'.",
        "BAIRROS: todas as UCs de BT (pessoa física e jurídica). GD do bairro = GDs dos trafos cujo bairro predominante é aquele. Potencial = consumo × (1 − penetração) ÷ 1150.",
        "USO: triagem comercial. Respeite a LGPD e as regras de contato (Não Perturbe) na abordagem.",
    ]})

    if a.site:
        n = salvar_site_emp(pd.concat([e_bt.assign(_mt=0), e_mt.assign(_mt=1)], ignore_index=True), a.site)
        log(f"Camada de prospecção do site: {n} municípios em {Path(a.site) / 'dados' / 'emp'}")

    saida = Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    arq = saida / f"Prospeccao_{a.mun or 'todos'}.xlsx"
    log(f"Salvando {arq} ...")
    with pd.ExcelWriter(arq, engine="openpyxl") as w:
        for nome, tab in (("Empresas BT", e_bt), ("Empresas MT", e_mt), ("Bairros", bai),
                          ("Atividades", atv), ("Municípios", mun), ("Leia-me", leiame)):
            tab = tab.drop(columns=[c for c in ("_gd", "_gdl") if c in tab.columns])
            for c in tab.columns:          # caracteres de controle (cadastro com erro) que o Excel não aceita
                if not pd.api.types.is_numeric_dtype(tab[c]):
                    tab[c] = tab[c].map(lambda v: ILEGAL.sub("", v) if isinstance(v, str) else v)
            tab = tab.round({c: 1 for c in tab.columns if pd.api.types.is_float_dtype(tab[c])
                             and c not in ("Latitude", "Longitude")})
            tab.to_excel(w, sheet_name=nome, index=False)
            ws = w.sheets[nome]
            ws.freeze_panes = "B2"
            ws.auto_filter.ref = ws.dimensions
            for j, col in enumerate(tab.columns, start=1):
                larg = max([len(str(col))] + [len(str(v)) for v in tab[col].head(100)])
                ws.column_dimensions[ws.cell(1, j).column_letter].width = min(140 if nome == "Leia-me" else 40, larg + 2)
    log(f"Pronto: {len(e_bt):,} empresas BT | {len(e_mt):,} empresas MT | {len(bai):,} bairros")


if __name__ == "__main__":
    main()
