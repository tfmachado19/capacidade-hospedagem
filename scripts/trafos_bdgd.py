#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trafos_bdgd.py — Carregamento estimado e GD instalada por transformador
de distribuição, a partir da BDGD (Base de Dados Geográfica da
Distribuidora) publicada nos dados abertos da ANEEL.

COMO USAR
  1. Baixe a BDGD da RGE no portal da ANEEL:
       https://dadosabertos-aneel.opendata.arcgis.com/search?tags=distribuicao
     (pesquise "RGE" e pegue a data-base mais recente; vem um .zip com um .gdb)
  2. Instale as bibliotecas (uma vez só):
       py -m pip install geopandas pyogrio openpyxl
  3. Rode (pode passar o .zip direto, sem descompactar):
       py trafos_bdgd.py "C:\\Downloads\\RGE_xxx.zip"
     Opções:
       --mun 4316907      código IBGE do município (padrão: Santa Maria/RS);
                          "todos" processa toda a área da distribuidora e gera um
                          site com seletor de município (pasta site_todos/)
       --fp 0.92          fator de potência p/ converter kW em kVA
       --janela 9 15      janela solar (h) p/ a carga mínima diurna
       --fator-seg 1.0    fator de segurança sobre a carga mínima
       --saida pasta      onde salvar (padrão: mesma pasta do zip)
       --gd-aneel arq     base oficial de GD da ANEEL (.parquet ou .csv) p/ a potência
                          instalada de cada UG, cruzada pelo CEG_GD (recomendado: na
                          BDGD da RGE o POT_INST vem como energia máx. mensal ÷ 720 h)
                          https://dadosabertos.aneel.gov.br/dataset/relacao-de-empreendimentos-de-geracao-distribuida

SAÍDA
  - BDGD_trafos_<mun>.xlsx  (Transformadores, Alimentadores, Resumo, Leia-me)
  - Capacidade_Hospedagem_<mun>.html  (plataforma de consulta: abre no navegador,
    busca por endereço, coordenada, trafo ou alimentador)

ATENÇÃO: os valores de carregamento e de carga mínima são ESTIMATIVAS feitas
a partir da energia mensal das UCs e das curvas típicas (CRVCRG) da própria
BDGD. Não são medição do transformador e não substituem a análise da RGE.
"""
import argparse
import gzip
import json
import re
import sys
import time
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import pyogrio
except ImportError:
    sys.exit("Falta a biblioteca pyogrio. Rode:  py -m pip install geopandas pyogrio openpyxl")

CAND = {
    "trafo": ["UNTRMT", "UNTRD"],
    "uc": ["UCBT_tab", "UCBT"],
    "ug": ["UGBT_tab", "UGBT"],
    "pip": ["PIP"],
    "crv": ["CRVCRG"],
    "ctmt": ["CTMT"],
    "ucmt": ["UCMT_tab", "UCMT"],
    "ugmt": ["UGMT_tab", "UGMT"],
    "ssdmt": ["SSDMT"],
    "segcon": ["SEGCON"],
    "ssdbt": ["SSDBT"],
    "ponnot": ["PONNOT"],
}
# Tabela TTEN da ANEEL (só as tensões de MT usadas nos alimentadores)
TTEN_KV = {"49": 13.8, "63": 23.0}
ENE = [f"ENE_{i:02d}" for i in range(1, 13)]
POT = [f"POT_{i:02d}" for i in range(1, 97)]
DIAS_MES = np.array([31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])
# Geração FV típica no RS (kWh/kWp por mês), escalada para ~1.150 kWh/kWp·ano
GER_FV = np.array([150, 130, 125, 100, 80, 65, 72, 92, 105, 128, 145, 155]) * 1150 / 1347
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
KVA_PADRAO = {3, 5, 10, 15, 25, 30, 37.5, 45, 50, 75, 100, 112.5, 150, 200, 225,
              250, 300, 400, 500, 750, 1000, 1500, 2000}
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


# ---------------------------------------------------------------- leitura
def abrir_gdb(caminho):
    p = Path(caminho)
    if p.is_file() and p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            gdbs = sorted({n.split(".gdb/")[0] + ".gdb" for n in z.namelist() if ".gdb/" in n})
        if not gdbs:
            sys.exit("Não achei nenhuma pasta .gdb dentro do zip.")
        return f"/vsizip/{p.resolve().as_posix()}/{gdbs[0]}", gdbs[0]
    if p.is_dir():
        if p.suffix.lower() == ".gdb":
            return str(p), p.name
        achados = sorted(p.rglob("*.gdb"))
        if achados:
            return str(achados[0]), achados[0].name
    sys.exit(f"Caminho inválido (esperava um .zip da BDGD ou uma pasta .gdb): {caminho}")


def achar_camada(camadas, chave, obrigatoria=True):
    nomes = {n.upper(): n for n in camadas}
    for c in CAND[chave]:
        if c.upper() in nomes:
            return nomes[c.upper()]
    if obrigatoria:
        sys.exit(f"Camada {CAND[chave]} não encontrada. Camadas do arquivo: {sorted(camadas)}")
    return None


def campos(gdb, camada):
    info = pyogrio.read_info(gdb, layer=camada)
    return dict(zip([f.upper() for f in info["fields"]], info["fields"])), \
        dict(zip([f.upper() for f in info["fields"]], info["dtypes"]))


def ler(gdb, camada, cols, where=None, geom=False):
    mapa, _ = campos(gdb, camada)
    usar = [mapa[c] for c in cols if c in mapa]
    df = pyogrio.read_dataframe(gdb, layer=camada, columns=usar,
                                read_geometry=geom, where=where)
    return df.rename(columns={mapa[c]: c for c in cols if c in mapa})


def col_trafo(df):
    for c in ("UNI_TR_MT", "UNI_TR_D"):
        if c in df.columns:
            return c
    return None


def txt(s):
    return s.astype(object).where(s.notna(), "").astype(str).str.strip().replace({"None": "", "nan": "", "<NA>": ""})


def ler_por_municipio(gdb, camada, cols, mun):
    """Tenta filtrar MUN direto no arquivo (rápido); se não der, filtra em memória.
    mun=None lê a área inteira."""
    if mun is None:
        return ler(gdb, camada, cols)
    _, tipos = campos(gdb, camada)
    if "MUN" in tipos:
        q = f"MUN = '{mun}'" if tipos["MUN"] == "object" else f"MUN = {int(mun)}"
        try:
            df = ler(gdb, camada, cols, where=q)
            if len(df):
                return df
        except Exception:
            pass
    df = ler(gdb, camada, cols)
    if "MUN" in df.columns:
        df = df[txt(df["MUN"]).str.lstrip("0") == str(mun).lstrip("0")]
    return df


# ---------------------------------------------------------------- curvas
def montar_curvas(gdb, camada_crv):
    """CRVCRG: 96 pontos (15 min) por tipologia e tipo de dia (DU/SA/DO).
    Normaliza pela média semanal ponderada (5 DU + SA + DO)/7 → média = 1."""
    if not camada_crv:
        return {}
    crv = ler(gdb, camada_crv, ["COD_ID", "TIP_DIA"] + POT)
    if crv.empty or not set(POT).issubset(crv.columns):
        return {}
    crv["COD_ID"] = txt(crv["COD_ID"])
    crv["TIP_DIA"] = txt(crv["TIP_DIA"]).str.upper()
    curvas = {}
    for cod, g in crv.groupby("COD_ID"):
        dias = {}
        for _, r in g.iterrows():
            v = pd.to_numeric(r[POT], errors="coerce").to_numpy(float)
            if np.isfinite(v).all() and v.sum() > 0:
                dias[r["TIP_DIA"]] = v
        if not dias:
            continue
        du = dias.get("DU", next(iter(dias.values())))
        sa, do = dias.get("SA", du), dias.get("DO", dias.get("SA", du))
        m = (5 * du.mean() + sa.mean() + do.mean()) / 7
        if m > 0:
            curvas[cod] = np.vstack([du, sa, do]) / m      # (3, 96)
    return curvas


# ---------------------------------------------------------------- cálculo
def agregar_carga(pares, curvas, janela, lote=20000):
    """Processa em lotes de trafos para não estourar a memória em áreas grandes."""
    trafos = pares["TRAFO"].unique()
    if len(trafos) <= lote:
        return _agregar_carga(pares, curvas, janela)
    pares = pares.sort_values("TRAFO", kind="stable").reset_index(drop=True)
    corte = np.searchsorted(pares["TRAFO"].to_numpy(), np.sort(trafos)[::lote][1:])
    limites = [0, *corte.tolist(), len(pares)]
    partes = [_agregar_carga(pares.iloc[i:j].reset_index(drop=True), curvas, janela)
              for i, j in zip(limites[:-1], limites[1:]) if j > i]
    return pd.concat(partes, ignore_index=True)


def _agregar_carga(pares, curvas, janela):
    """pares: DataFrame com TRAFO, TIP_CC, P_01..P_12 (kW médio por mês).
    Retorna por trafo: pico estimado (mês de maior energia) e carga mínima
    na janela solar (mês de menor energia)."""
    P = pares[[f"P_{i:02d}" for i in range(1, 13)]].to_numpy(float)
    trafos, idx = np.unique(pares["TRAFO"].to_numpy(), return_inverse=True)
    n = len(trafos)
    soma_mes = np.zeros((n, 12))
    np.add.at(soma_mes, idx, P)
    m_max = soma_mes.argmax(axis=1)
    m_min = np.where(soma_mes.sum(axis=1) > 0, soma_mes.argmin(axis=1), 0)

    plano = np.ones((3, 96))
    forma = np.stack([curvas.get(t, plano) for t in pares["TIP_CC"].to_numpy()])   # (k,3,96)
    p_max = P[np.arange(len(P)), m_max[idx]]
    p_min = P[np.arange(len(P)), m_min[idx]]
    curva_max = np.zeros((n, 3, 96))
    curva_min = np.zeros((n, 3, 96))
    np.add.at(curva_max, idx, forma * p_max[:, None, None])
    np.add.at(curva_min, idx, forma * p_min[:, None, None])

    i0, i1 = int(janela[0] * 4), int(janela[1] * 4)
    pico = curva_max.reshape(n, -1).max(axis=1)
    hora_pico = (curva_max.max(axis=1).argmax(axis=1)) / 4
    cmin = curva_min[:, :, i0:i1].reshape(n, -1).min(axis=1)
    sem_curva = pares.assign(_s=pares["N"].where(~pares["TIP_CC"].isin(curvas.keys()), 0)) \
        .groupby("TRAFO")["_s"].sum()
    return pd.DataFrame({
        "TRAFO": trafos,
        "Energia média mensal (kWh)": (soma_mes * DIAS_MES * 24).mean(axis=1),
        "Energia mês máx (kWh)": (soma_mes * DIAS_MES * 24).max(axis=1),
        "Mês de maior consumo": [MESES[m] for m in m_max],
        "Demanda pico estimada (kW)": pico,
        "Hora do pico": [f"{int(h):02d}:{int(round((h % 1) * 60)):02d}" for h in hora_pico],
        "Carga mín. diurna estimada (kW)": cmin,
        "Mês de menor consumo": [MESES[m] for m in m_min],
        "_pmed_min": soma_mes[np.arange(n), m_min],
    }).merge(sem_curva.rename("UCs sem curva típica").reset_index(), on="TRAFO", how="left")


def ler_gd_aneel(arq):
    """Base 'Relação de empreendimentos de GD' da ANEEL → Série CEG → kW instalado."""
    cols = ["CodEmpreendimento", "MdaPotenciaInstaladaKW"]
    if str(arq).lower().endswith(".parquet"):
        d = pd.read_parquet(arq, columns=cols)
    else:
        d = pd.read_csv(arq, sep=";", decimal=",", usecols=cols, encoding="latin-1", dtype=str)
    d["CodEmpreendimento"] = txt(d["CodEmpreendimento"])
    d["MdaPotenciaInstaladaKW"] = pd.to_numeric(
        d["MdaPotenciaInstaladaKW"].astype(str).str.replace(",", "."), errors="coerce")
    return d.dropna().drop_duplicates("CodEmpreendimento").set_index("CodEmpreendimento")["MdaPotenciaInstaladaKW"]


def nomes_ibge():
    """Código IBGE → (nome, UF) pela API do IBGE. Sem internet, devolve {}."""
    try:
        url = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado"
        with urllib.request.urlopen(url, timeout=30) as r:
            bruto = r.read()
        if bruto[:2] == b"\x1f\x8b":                    # a API responde em gzip
            bruto = gzip.decompress(bruto)
        return {str(m["municipio-id"]): (m["municipio-nome"], m["UF-sigla"]) for m in json.loads(bruto)}
    except Exception as e:
        log(f"  (nomes dos municípios indisponíveis: {e})")
        return {}


def gd_alimentadores(gdb, L, gdmap, ctmts=None):
    """GD ligada em cada alimentador, em BT (UGBT) e MT (UGMT), em kW.
    Potência: base de GD da ANEEL via CEG_GD; sem casamento, BT usa POT_INST e MT usa DEM_CONT
    (usinas fora do cadastro de GD, como PCH/UHE/UTE, têm DEM_CONT = potência de injeção)."""
    filtro = None if ctmts is None else "CTMT IN (" + ",".join(f"'{c}'" for c in sorted(ctmts)) + ")"
    out = {}
    for chave, rot in (("ug", "BT"), ("ugmt", "MT")):
        if not L.get(chave):
            continue
        try:
            u = ler(gdb, L[chave], ["CTMT", "CEG_GD", "POT_INST", "DEM_CONT", "SIT_ATIV"], where=filtro)
        except Exception:
            continue
        if "SIT_ATIV" in u.columns:
            u = u[txt(u["SIT_ATIV"]).str.upper().isin(["AT", ""])]
        pot = pd.to_numeric(u.get("POT_INST"), errors="coerce").fillna(0)
        if rot == "BT":
            pot = pot.where(pot <= 1000, pot / 1000)
            alt = pot
        else:
            dem = pd.to_numeric(u.get("DEM_CONT"), errors="coerce").fillna(0)
            alt = dem.where(dem > 0, pot)
        kw = pd.Series(np.nan, index=u.index)
        if gdmap is not None and "CEG_GD" in u.columns:
            ceg = txt(u["CEG_GD"])
            kw = ceg.map(gdmap) / ceg.map(ceg.value_counts())
        u = u.assign(KW=kw.fillna(alt), CTMT=txt(u["CTMT"]))
        out[f"GD no alimentador — {rot} (kW)"] = u.groupby("CTMT")["KW"].sum()
    if not out:
        return pd.DataFrame(columns=["Alimentador (CTMT)"])
    d = pd.DataFrame(out).fillna(0)
    d["GD no alimentador (kW)"] = d.sum(axis=1)
    return d.rename_axis("Alimentador (CTMT)").reset_index()


def calc_alimentadores(gdb, L, curvas, janela, fp, ctmts=None, gd_kw=None):
    """Carregamento estimado de cada alimentador (CTMT).
    Demanda: energia injetada no mês de maior energia (CTMT.ENE_xx) → kW médio, × fator de pico
    da soma das curvas típicas das cargas do alimentador (UCs de BT e MT e iluminação pública).
    Capacidade: corrente nominal (SEGCON.CNOM) do condutor do trecho de saída (SSDMT ligado ao
    PAC_INI), × tensão nominal (TEN_NOM) × √3."""
    if not L.get("ctmt"):
        return pd.DataFrame()
    ct = ler(gdb, L["ctmt"], ["COD_ID", "PAC_INI", "TEN_NOM"] + ENE)
    ct["COD_ID"] = txt(ct["COD_ID"])
    if ctmts is not None:
        ct = ct[ct["COD_ID"].isin(ctmts)]
    if ct.empty:
        return pd.DataFrame()
    filtro = None if ctmts is None else "CTMT IN (" + ",".join(f"'{c}'" for c in sorted(ct["COD_ID"])) + ")"
    for c in ENE:
        ct[c] = pd.to_numeric(ct.get(c, 0), errors="coerce").fillna(0)
    pmed = ct[ENE].to_numpy(float) / (DIAS_MES * 24)
    ct["PMED"] = pmed.max(axis=1)

    # fator de pico pelas curvas típicas das cargas do alimentador
    partes = []
    for chave in ("uc", "ucmt", "pip"):
        if not L.get(chave):
            continue
        try:
            d = ler(gdb, L[chave], ["CTMT", "SIT_ATIV", "TIP_CC"] + ENE, where=filtro)
        except Exception:
            continue
        if "SIT_ATIV" in d.columns:
            d = d[txt(d["SIT_ATIV"]).str.upper().isin(["AT", ""])]
        d["TRAFO"] = txt(d["CTMT"])
        d = d[d["TRAFO"].isin(set(ct["COD_ID"]))]
        d["TIP_CC"] = txt(d["TIP_CC"]) if "TIP_CC" in d.columns else ""
        for c in ENE:
            d[c] = pd.to_numeric(d.get(c, 0), errors="coerce").fillna(0)
        partes.append(d[["TRAFO", "TIP_CC"] + ENE])
    fc = pd.Series(dtype=float)
    if partes:
        cg = pd.concat(partes, ignore_index=True)
        pares = pd.DataFrame(cg[ENE].to_numpy(float) / (DIAS_MES * 24), columns=[f"P_{i:02d}" for i in range(1, 13)])
        pares["TRAFO"], pares["TIP_CC"], pares["N"] = cg["TRAFO"].to_numpy(), cg["TIP_CC"].to_numpy(), 1
        r = agregar_carga(pares.groupby(["TRAFO", "TIP_CC"], as_index=False).sum(), curvas, janela)
        mes = r["Mês de maior consumo"].map(MESES.index).to_numpy()
        base = r["Energia mês máx (kWh)"] / (DIAS_MES[mes] * 24)
        fc = (r["Demanda pico estimada (kW)"] / base.where(base > 0)).set_axis(r["TRAFO"])
        pico_cargas = r.set_index("TRAFO")["Demanda pico estimada (kW)"]
        rr = r.set_index("TRAFO")
        cmin_cargas = rr["Carga mín. diurna estimada (kW)"]
        fmin = cmin_cargas / rr["_pmed_min"].where(rr["_pmed_min"] > 0)     # mín. na janela ÷ média
    else:
        pico_cargas = cmin_cargas = fmin = pd.Series(dtype=float)
    # carga mínima diurna: carga bruta = energia medida no alimentador (já líquida da GD) +
    # geração estimada da GD do alimentador; × forma das curvas (mín. na janela solar ÷ média)
    gkw = ct["COD_ID"].map(gd_kw).fillna(0).to_numpy() if gd_kw is not None else np.zeros(len(ct))
    bruta = pmed + gkw[:, None] * GER_FV / (DIAS_MES * 24)
    ct["CMIN"] = (bruta.min(axis=1) * ct["COD_ID"].map(fmin)).where(ct["PMED"] > 0, ct["COD_ID"].map(cmin_cargas))
    ct["FC"] = ct["COD_ID"].map(fc)
    ct["PICO"] = (ct["PMED"] * ct["FC"]).where(ct["PMED"] > 0, ct["COD_ID"].map(pico_cargas))

    # capacidade do trecho de saída
    ct["I"] = np.nan
    if L.get("ssdmt") and L.get("segcon"):
        try:
            sd = ler(gdb, L["ssdmt"], ["CTMT", "PAC_1", "PAC_2", "TIP_CND"], where=filtro)
            sc = ler(gdb, L["segcon"], ["COD_ID", "CNOM"])
            sc["COD_ID"] = txt(sc["COD_ID"])
            ini = set(txt(ct["PAC_INI"]))
            sd = sd[txt(sd["PAC_1"]).isin(ini) | txt(sd["PAC_2"]).isin(ini)]
            sd = sd.assign(CTMT=txt(sd["CTMT"]), TIP_CND=txt(sd["TIP_CND"])).merge(
                sc, left_on="TIP_CND", right_on="COD_ID", how="left")
            corr = pd.to_numeric(sd["CNOM"], errors="coerce")
            ct["I"] = ct["COD_ID"].map(corr.groupby(sd["CTMT"]).min())
        except Exception as e:
            log(f"  (capacidade dos alimentadores indisponível: {e})")
    ct["KV"] = txt(ct["TEN_NOM"]).map(TTEN_KV)
    ct["MVA"] = np.sqrt(3) * ct["KV"] * ct["I"] / 1000
    ct["CAR"] = ct["PICO"] / fp / (ct["MVA"] * 1000) * 100
    return pd.DataFrame({
        "Alimentador (CTMT)": ct["COD_ID"],
        "Tensão nominal (kV)": ct["KV"],
        "Corrente nominal do trecho de saída (A)": ct["I"],
        "Capacidade do alimentador (MVA)": ct["MVA"],
        "Energia injetada mês máx (MWh)": ct[ENE].max(axis=1) / 1000,
        "Demanda pico estimada do alimentador (kW)": ct["PICO"],
        "Carregamento do alimentador (%)": ct["CAR"],
        "Carga mín. diurna estimada do alimentador (kW)": ct["CMIN"],
    })


def classe_carreg(x):
    if pd.isna(x):
        return "sem dado"
    return "sobrecarga" if x > 100 else ("alto" if x >= 80 else "normal")


def classe_inv(gd, cmin, atencao):
    if gd <= 0:
        return "sem GD"
    if pd.isna(cmin) or cmin <= 0:
        return "sem dado"
    o = gd / cmin * 100
    return "risco" if o >= 100 else ("atenção" if o >= atencao else "folga")


# ---------------------------------------------------------------- mapa
def _n(v, d=4):
    return None if pd.isna(v) else round(float(v), d)


def _js(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _bbox(pts):
    return [round(pts["lat"].min() - 0.02, 4), round(pts["lon"].min() - 0.02, 4),
            round(pts["lat"].max() + 0.02, 4), round(pts["lon"].max() + 0.02, 4)]


def _registros(pts):
    n = _n
    return [{
        "id": str(r["Transformador (COD_ID)"]), "lat": round(r["lat"], 6), "lon": round(r["lon"], 6),
        "kva": n(r["Potência nominal (kVA)"]), "gd": n(r["GD instalada (kW)"]) or 0.0,
        "cmin": n(r["Carga mín. diurna estimada (kW)"]), "pico": n(r["Demanda pico estimada (kW)"]),
        "car": n(r["Carregamento estimado (%)"], 2),
        "alim": "" if pd.isna(r["Alimentador (CTMT)"]) else str(r["Alimentador (CTMT)"]),
        "sub": "" if pd.isna(r.get("Subestação")) else str(r.get("Subestação")),
        "ucs": int(r["Nº UCs"]), "ugs": int(r["Nº UGs (GD)"]),
        "ca": n(r.get("Carregamento do alimentador (%)"), 1),
        "gda": n(r.get("GD no alimentador (kW)"), 0), "cga": n(r.get("Carregamento de GD do alimentador (%)"), 1),
        "capa": n(r.get("Capacidade disponível p/ GD no alimentador (kW)"), 0),
    } for _, r in pts.iterrows()]


def _html(dados_js, meta, titulo):
    return (TEMPLATE.replace("__DATA__", dados_js).replace("__META__", _js(meta))
            .replace("__MUNNOME__", titulo).replace("__BASE__", meta["base"]))


def salvar_plataforma(df, arq, meta):
    """Gera a plataforma de consulta (HTML único) com os dados embutidos."""
    pts = df.dropna(subset=["lat", "lon"])
    if len(pts):
        meta["bbox"] = _bbox(pts)
    Path(arq).write_text(_html(_js(_registros(pts)), meta, meta["munNome"].split(",")[0]), encoding="utf-8")


def nome_arq(c):
    return re.sub(r"[^\w.-]", "_", str(c)) or "_"


def salvar_redes_bt(gdb, L, pasta, df, ug):
    """dados/bt/<alimentador>.json: para cada trafo, a rede BT (SSDBT) e os postes com GD (UGBT → PONNOT).
    Linhas: [x0, y0, dx1, dy1, ...] em graus × 1e5 (≈ 1 m), com diferenças entre pontos para encolher.
    GD: [lat, lon, kW, nº de UGs] por poste."""
    import shapely
    if not L.get("ssdbt"):
        log("  (sem camada SSDBT: rede BT não gerada)")
        return 0
    pasta = Path(pasta) / "dados" / "bt"
    pasta.mkdir(parents=True, exist_ok=True)
    alim_de = dict(zip(df["Transformador (COD_ID)"].astype(str), df["Alimentador (CTMT)"].fillna("").astype(str)))
    redes = {}
    sd = ler(gdb, L["ssdbt"], ["UNI_TR_MT", "COMP"], geom=True)
    sd["UNI_TR_MT"] = txt(sd["UNI_TR_MT"])
    sd = sd[sd["UNI_TR_MT"].isin(alim_de.keys())]
    if sd.crs is not None and sd.crs.to_epsg() != 4326:
        sd = sd.to_crs(4326)
    sd = sd.explode(index_parts=False).reset_index(drop=True)
    xy, idx = shapely.get_coordinates(sd.geometry.values, return_index=True)
    q = np.round(xy * 1e5).astype(np.int64)
    corte = np.flatnonzero(np.diff(idx)) + 1
    for tr_id, pts in zip(sd["UNI_TR_MT"].to_numpy()[idx[np.r_[0, corte]]] if len(idx) else [], np.split(q, corte)):
        d = np.diff(pts, axis=0)
        redes.setdefault(tr_id, {"l": [], "g": [], "km": 0.0})["l"].append(
            [int(pts[0, 0]), int(pts[0, 1])] + d.ravel().tolist())
    km = pd.to_numeric(sd["COMP"], errors="coerce").groupby(sd["UNI_TR_MT"]).sum() / 1000
    for k, v in km.items():
        redes[k]["km"] = round(float(v), 2)
    if L.get("ponnot") and "PN_CON" in ug.columns:
        pn = ler(gdb, L["ponnot"], ["COD_ID"], geom=True)
        if pn.crs is not None and pn.crs.to_epsg() != 4326:
            pn = pn.to_crs(4326)
        pn = pd.DataFrame({"PN": txt(pn["COD_ID"]), "lat": pn.geometry.y, "lon": pn.geometry.x})
        g = ug.assign(PN=txt(ug["PN_CON"])).merge(pn, on="PN", how="inner")
        g = g[g["TRAFO"].isin(alim_de.keys())]
        g = g.groupby(["TRAFO", "PN"]).agg(lat=("lat", "first"), lon=("lon", "first"),
                                           kw=("POT_KW", "sum"), n=("POT_KW", "size")).reset_index()
        for r in g.itertuples(index=False):
            redes.setdefault(r.TRAFO, {"l": [], "g": [], "km": 0.0})["g"].append(
                [round(r.lat, 6), round(r.lon, 6), round(float(r.kw), 2), int(r.n)])
    por_alim = {}
    for tr_id, v in redes.items():
        por_alim.setdefault(nome_arq(alim_de.get(tr_id, "")), {})[tr_id] = v
    for a, d in por_alim.items():
        (pasta / f"{a}.json").write_text(_js(d), encoding="utf-8")
    return len(redes)


def salvar_redes_mt(gdb, L, pasta, tol=0.00003):
    """dados/mt/<alimentador>.json: traçado da rede MT (SSDMT) de cada alimentador, simplificado (~3 m),
    no mesmo formato compacto da rede BT: {"l": [[x0, y0, dx1, dy1, ...], ...], "km": comprimento}."""
    import shapely
    if not L.get("ssdmt"):
        return 0
    pasta = Path(pasta) / "dados" / "mt"
    pasta.mkdir(parents=True, exist_ok=True)
    sd = ler(gdb, L["ssdmt"], ["CTMT", "COMP"], geom=True)
    sd["CTMT"] = txt(sd["CTMT"])
    sd = sd[sd["CTMT"] != ""]
    if sd.crs is not None and sd.crs.to_epsg() != 4326:
        sd = sd.to_crs(4326)
    km = pd.to_numeric(sd["COMP"], errors="coerce").groupby(sd["CTMT"]).sum() / 1000
    sd = sd.explode(index_parts=False).reset_index(drop=True)
    geo = shapely.simplify(sd.geometry.values, tol)
    xy, idx = shapely.get_coordinates(geo, return_index=True)
    q = np.round(xy * 1e5).astype(np.int64)
    corte = np.flatnonzero(np.diff(idx)) + 1
    redes = {}
    if len(idx):
        for ct, pts in zip(sd["CTMT"].to_numpy()[idx[np.r_[0, corte]]], np.split(q, corte)):
            redes.setdefault(ct, []).append([int(pts[0, 0]), int(pts[0, 1])] + np.diff(pts, axis=0).ravel().tolist())
    for ct, linhas in redes.items():
        (pasta / f"{nome_arq(ct)}.json").write_text(
            _js({"l": linhas, "km": round(float(km.get(ct, 0)), 1)}), encoding="utf-8")
    return len(redes)


def salvar_site(df, pasta, meta, padrao="4316907"):
    """Área inteira: index.html + dados/<código IBGE>.json (carregado sob demanda por município).
    Precisa ser servido por HTTP (GitHub Pages); aberto direto do disco o navegador bloqueia o fetch."""
    pasta = Path(pasta)
    (pasta / "dados").mkdir(parents=True, exist_ok=True)
    pts = df.dropna(subset=["lat", "lon"])
    muns = []
    for (cod, nome), g in pts.groupby(["Município (IBGE)", "Município"]):
        (pasta / "dados" / f"{cod}.json").write_text(_js(_registros(g)), encoding="utf-8")
        muns.append({"c": str(cod), "n": str(nome), "b": _bbox(g), "t": len(g),
                     "gd": round(float(g["GD instalada (kW)"].sum()), 1)})
    chave = lambda m: unicodedata.normalize("NFKD", m["n"]).encode("ascii", "ignore").decode().lower()
    muns.sort(key=chave)
    meta.update(muns=muns, bboxAll=_bbox(pts), bbox=None, mun=None,
                default=padrao if any(m["c"] == padrao for m in muns) else muns[0]["c"])
    (pasta / "index.html").write_text(_html("null", meta, meta.get("titulo", "Área de concessão")),
                                      encoding="utf-8")
    return len(muns)


# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser(description="Carregamento e GD por transformador (BDGD/ANEEL)")
    ap.add_argument("bdgd", help="arquivo .zip da BDGD ou pasta .gdb")
    ap.add_argument("--mun", default="4316907", help="código IBGE do município (padrão Santa Maria/RS)")
    ap.add_argument("--fp", type=float, default=0.92, help="fator de potência (padrão 0,92)")
    ap.add_argument("--janela", type=float, nargs=2, default=[9, 15], metavar=("INI", "FIM"))
    ap.add_argument("--fator-seg", type=float, default=1.0, help="fator de segurança sobre a carga mínima")
    ap.add_argument("--atencao", type=float, default=60.0, help="limiar de 'atenção' da ocupação (%%)")
    ap.add_argument("--saida", default=None, help="pasta de saída")
    ap.add_argument("--gd-aneel", default=None,
                    help="base de GD da ANEEL (.parquet/.csv) p/ potência instalada via CEG_GD")
    ap.add_argument("--mun-nome", default=None, help="nome do município p/ busca de endereço (ex.: \"Itaara, RS\"); "
                    "com --mun todos, título do site (ex.: \"RGE\")")
    a = ap.parse_args()
    todos = a.mun.strip().lower() in ("todos", "all", "*")
    mun = None if todos else a.mun.strip()
    nomes = nomes_ibge()
    if not nomes and a.gd_aneel:                       # sem IBGE: nomes da base de GD da ANEEL
        try:
            cols = ["CodMunicipioIbge", "NomMunicipio", "SigUF"]
            d = pd.read_parquet(a.gd_aneel, columns=cols) if str(a.gd_aneel).lower().endswith(".parquet") \
                else pd.read_csv(a.gd_aneel, sep=";", usecols=cols, encoding="latin-1", dtype=str)
            d = d.dropna().drop_duplicates("CodMunicipioIbge")
            nomes = {str(int(c)): (n, u) for c, n, u in zip(d["CodMunicipioIbge"], d["NomMunicipio"], d["SigUF"])}
            log(f"  nomes de {len(nomes):,} municípios pela base de GD da ANEEL")
        except Exception as e:
            log(f"  (nomes pela base ANEEL indisponíveis: {e})")

    gdb, nome_gdb = abrir_gdb(a.bdgd)
    saida = Path(a.saida) if a.saida else Path(a.bdgd).resolve().parent
    saida.mkdir(parents=True, exist_ok=True)
    camadas = [c[0] for c in pyogrio.list_layers(gdb)]
    L = {k: achar_camada(camadas, k, k in ("trafo", "uc", "ug")) for k in CAND}
    log(f"BDGD: {nome_gdb} | camadas: trafo={L['trafo']} uc={L['uc']} ug={L['ug']} "
        f"pip={L['pip']} curvas={L['crv']}")

    # 1) Unidades consumidoras BT do município
    log(f"Lendo UCs de BT {'de toda a área' if todos else 'do município ' + a.mun} ...")
    uc = ler_por_municipio(gdb, L["uc"], ["COD_ID", "MUN", "SIT_ATIV", "UNI_TR_MT", "UNI_TR_D",
                                          "CTMT", "TIP_CC", "CEG_GD"] + ENE, mun)
    ct = col_trafo(uc)
    if uc.empty or not ct:
        sys.exit("Nenhuma UC encontrada para esse município (confira o código IBGE em --mun).")
    if "SIT_ATIV" in uc.columns:
        uc = uc[txt(uc["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    uc["TRAFO"] = txt(uc[ct])
    uc = uc[uc["TRAFO"] != ""]
    uc["TIP_CC"] = txt(uc["TIP_CC"]) if "TIP_CC" in uc.columns else ""
    for c in ENE:
        uc[c] = pd.to_numeric(uc.get(c, 0), errors="coerce").fillna(0)
    trafos_mun = set(uc["TRAFO"])
    log(f"  {len(uc):,} UCs ativas em {len(trafos_mun):,} transformadores")

    # 2) Iluminação pública ligada nesses trafos (opcional)
    partes = [uc[["TRAFO", "TIP_CC"] + ENE]]
    if L["pip"]:
        try:
            pip = ler(gdb, L["pip"], ["UNI_TR_MT", "UNI_TR_D", "SIT_ATIV", "TIP_CC"] + ENE)
            cp = col_trafo(pip)
            if cp and not pip.empty:
                pip["TRAFO"] = txt(pip[cp])
                pip = pip[pip["TRAFO"].isin(trafos_mun)]
                pip["TIP_CC"] = txt(pip["TIP_CC"]) if "TIP_CC" in pip.columns else ""
                for c in ENE:
                    pip[c] = pd.to_numeric(pip.get(c, 0), errors="coerce").fillna(0)
                partes.append(pip[["TRAFO", "TIP_CC"] + ENE])
                log(f"  + {len(pip):,} pontos de iluminação pública")
        except Exception as e:
            log(f"  (iluminação pública ignorada: {e})")
    cargas = pd.concat(partes, ignore_index=True)

    # 3) Geração distribuída em BT
    log("Lendo unidades geradoras (GD) de BT ...")
    ug = ler(gdb, L["ug"], ["COD_ID", "MUN", "SIT_ATIV", "UNI_TR_MT", "UNI_TR_D", "CTMT",
                            "POT_INST", "CEG_GD", "DAT_CON", "PN_CON"] + ENE)
    cg = col_trafo(ug)
    ug["TRAFO"] = txt(ug[cg]) if cg else ""
    if "SIT_ATIV" in ug.columns:
        ug = ug[txt(ug["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    no_mun = True if todos else (txt(ug["MUN"]).str.lstrip("0") == mun.lstrip("0")
                                 if "MUN" in ug.columns else False)
    ug = ug[ug["TRAFO"].isin(trafos_mun) | no_mun].copy()
    ug["POT_KW"] = pd.to_numeric(ug["POT_INST"], errors="coerce").fillna(0)
    corrigidas = int((ug["POT_KW"] > 1000).sum())
    ug.loc[ug["POT_KW"] > 1000, "POT_KW"] /= 1000      # BT ≤ 75 kW: acima de 1000 está em W
    # Na BDGD da RGE o POT_INST costuma ser a energia do melhor mês ÷ 720 h (geração média),
    # não a potência instalada. Detecta isso e, se houver, usa a base oficial de GD da ANEEL.
    ene_max = ug[[c for c in ENE if c in ug.columns]].apply(pd.to_numeric, errors="coerce").max(axis=1)
    pct_energia = float((((ug["POT_KW"] - ene_max / 720).abs() < 0.01) & (ene_max > 0)).mean() * 100) \
        if len(ug) else 0.0
    fonte_gd, casadas = "POT_INST da BDGD", 0
    gdmap = ler_gd_aneel(a.gd_aneel) if a.gd_aneel else None
    if a.gd_aneel:
        ceg = txt(ug["CEG_GD"]) if "CEG_GD" in ug.columns else pd.Series("", index=ug.index)
        kw = ceg.map(gdmap)
        kw = kw / ceg.map(ceg.value_counts())          # mesmo CEG em várias UGs: divide
        casadas = int(kw.notna().sum())
        ug["POT_KW"] = kw.fillna(ug["POT_KW"])
        fonte_gd = f"base de GD da ANEEL via CEG_GD ({casadas:,} de {len(ug):,} UGs; demais: POT_INST)"
        log(f"  potência instalada da base ANEEL em {casadas:,} de {len(ug):,} UGs")
    elif pct_energia > 50:
        log(f"  ATENÇÃO: {pct_energia:.0f}% dos POT_INST = energia máx. mensal ÷ 720 h (não é potência "
            "instalada). Rode com --gd-aneel <base de GD da ANEEL> para corrigir.")
    trafos_mun |= set(ug["TRAFO"]) - {""}
    log(f"  {len(ug):,} UGs, {ug['POT_KW'].sum():,.1f} kW"
        + (f" ({corrigidas} valores corrigidos de W para kW)" if corrigidas else ""))

    # 4) Transformadores
    log("Lendo transformadores ...")
    tr = ler(gdb, L["trafo"], ["COD_ID", "POT_NOM", "CTMT", "SUB", "SIT_ATIV", "FAS_CON_P",
                               "FAS_CON_S", "TEN_LIN_SE", "TIP_TRAFO", "MUN"], geom=True)
    tr["COD_ID"] = txt(tr["COD_ID"])
    tr = tr[tr["COD_ID"].isin(trafos_mun)].copy()
    if "SIT_ATIV" in tr.columns:
        tr = tr[txt(tr["SIT_ATIV"]).str.upper().isin(["AT", ""])]
    try:
        if tr.crs is not None and tr.crs.to_epsg() != 4326:
            tr = tr.to_crs(4326)
        tr["lon"], tr["lat"] = tr.geometry.x, tr.geometry.y
    except Exception:
        tr["lon"] = tr["lat"] = np.nan
    tr["KVA"] = pd.to_numeric(tr["POT_NOM"], errors="coerce")
    tr["MUN_TR"] = txt(tr["MUN"]).str.lstrip("0") if "MUN" in tr.columns else (mun or "")
    tr["NOME_MUN"] = tr["MUN_TR"].map(lambda c: nomes.get(c, (c, ""))[0])
    pct_padrao = tr["KVA"].isin(KVA_PADRAO).mean() * 100 if len(tr) else 0
    log(f"  {len(tr):,} transformadores; {pct_padrao:.0f}% com potência em valores comerciais de kVA")

    # 5) Curvas típicas e carga por trafo
    log("Montando curvas de carga ...")
    curvas = montar_curvas(gdb, L["crv"])
    log(f"  {len(curvas)} tipologias de curva")
    P = cargas[ENE].to_numpy(float) / (DIAS_MES * 24)          # kWh/mês → kW médio
    pares = pd.DataFrame(P, columns=[f"P_{i:02d}" for i in range(1, 13)])
    pares["TRAFO"], pares["TIP_CC"] = cargas["TRAFO"].to_numpy(), cargas["TIP_CC"].to_numpy()
    pares["N"] = 1
    pares = pares.groupby(["TRAFO", "TIP_CC"], as_index=False).sum()
    carga = agregar_carga(pares, curvas, a.janela)

    log("Calculando carregamento dos alimentadores ...")
    cts = None if todos else set(txt(tr["CTMT"])) - {""}
    tab_gd = gd_alimentadores(gdb, L, gdmap, cts)
    tab_ct = calc_alimentadores(gdb, L, curvas, a.janela, a.fp, cts,
                                tab_gd.set_index("Alimentador (CTMT)")["GD no alimentador (kW)"]
                                if len(tab_gd) else None)
    if len(tab_ct):
        tab_ct = tab_ct.merge(tab_gd, on="Alimentador (CTMT)", how="left")
        for c in [c for c in tab_ct.columns if c.startswith("GD no alimentador")]:
            tab_ct[c] = tab_ct[c].fillna(0)
        gda = tab_ct["GD no alimentador (kW)"]
        cap_kw = tab_ct["Capacidade do alimentador (MVA)"] * 1000
        cmin_a = tab_ct["Carga mín. diurna estimada do alimentador (kW)"] * a.fator_seg
        tab_ct["Carregamento de GD do alimentador (%)"] = gda / cap_kw * 100
        tab_ct["Ocupação GD / carga mín. do alimentador (%)"] = gda / cmin_a.where(cmin_a > 0) * 100
        tab_ct["Margem p/ GD sem inversão no alimentador (kW)"] = cmin_a - gda
        tab_ct["Margem térmica p/ GD no alimentador (kW)"] = cap_kw - gda
        tab_ct["Capacidade disponível p/ GD no alimentador (kW)"] = np.fmin(
            tab_ct["Margem p/ GD sem inversão no alimentador (kW)"],
            tab_ct["Margem térmica p/ GD no alimentador (kW)"]).clip(lower=0)
        log(f"  GD nos alimentadores: {gda.sum() / 1000:,.1f} MW (BT+MT); carregamento de GD mediano "
            f"{tab_ct['Carregamento de GD do alimentador (%)'].median():.0f}%")
        log(f"  {len(tab_ct):,} alimentadores; {tab_ct['Carregamento do alimentador (%)'].notna().sum():,} "
            f"com carregamento; mediana {tab_ct['Carregamento do alimentador (%)'].median():.0f}%")

    # 6) Junta tudo
    n_uc = uc.groupby("TRAFO").size().rename("Nº UCs")
    n_ucgd = uc[txt(uc.get("CEG_GD", pd.Series("", index=uc.index))) != ""].groupby("TRAFO").size() \
        .rename("Nº UCs com GD")
    gd = ug.groupby("TRAFO").agg(**{"Nº UGs (GD)": ("POT_KW", "size"),
                                    "GD instalada (kW)": ("POT_KW", "sum")})
    df = tr.drop(columns="geometry", errors="ignore").rename(columns={"COD_ID": "TRAFO"})
    df = df.merge(n_uc, on="TRAFO", how="left").merge(n_ucgd, on="TRAFO", how="left") \
        .merge(gd, on="TRAFO", how="left").merge(carga, on="TRAFO", how="left")
    for c in ("Nº UCs", "Nº UCs com GD", "Nº UGs (GD)", "GD instalada (kW)", "UCs sem curva típica"):
        df[c] = df[c].fillna(0)

    kva = df["KVA"].where(df["KVA"] > 0)
    df["Carregamento estimado (%)"] = df["Demanda pico estimada (kW)"] / a.fp / kva * 100
    df["Situação carregamento"] = df["Carregamento estimado (%)"].map(classe_carreg)
    df["CTMT"] = txt(df["CTMT"]) if "CTMT" in df.columns else ""
    for c in ("Carregamento do alimentador (%)", "Carregamento de GD do alimentador (%)",
              "GD no alimentador (kW)", "Capacidade disponível p/ GD no alimentador (kW)"):
        df[c] = df["CTMT"].map(tab_ct.set_index("Alimentador (CTMT)")[c]) \
            if len(tab_ct) and c in tab_ct.columns else np.nan
    df["GD / potência do trafo (%)"] = df["GD instalada (kW)"] / kva * 100
    cmin = df["Carga mín. diurna estimada (kW)"] * a.fator_seg
    df["Ocupação GD / carga mín. (%)"] = (df["GD instalada (kW)"] / cmin.where(cmin > 0) * 100) \
        .where(df["GD instalada (kW)"] > 0)
    df["Margem p/ nova GD sem inversão (kW)"] = cmin - df["GD instalada (kW)"]
    df["Situação inversão de fluxo"] = [classe_inv(g, c, a.atencao)
                                       for g, c in zip(df["GD instalada (kW)"], cmin)]
    # mesma regra da plataforma: menor entre (carga mín. × fs − GD) e (kVA − GD), nunca negativa
    df["Capacidade disponível estimada (kW)"] = np.fmin(cmin - df["GD instalada (kW)"],
                                                        kva - df["GD instalada (kW)"]).clip(lower=0)
    df["Google Maps"] = [f"https://www.google.com/maps?q={la:.6f},{lo:.6f}" if pd.notna(la) else ""
                         for la, lo in zip(df["lat"], df["lon"])]

    ren = {"TRAFO": "Transformador (COD_ID)", "CTMT": "Alimentador (CTMT)", "SUB": "Subestação",
           "KVA": "Potência nominal (kVA)", "FAS_CON_P": "Fases primário", "POT_NOM": "POT_NOM (bruto)",
           "MUN_TR": "Município (IBGE)", "NOME_MUN": "Município"}
    df = df.rename(columns=ren)
    ordem = ["Transformador (COD_ID)", "Município (IBGE)", "Município", "Alimentador (CTMT)", "Subestação",
             "Potência nominal (kVA)",
             "Fases primário", "Nº UCs", "Nº UCs com GD", "Nº UGs (GD)", "GD instalada (kW)",
             "GD / potência do trafo (%)", "Energia média mensal (kWh)", "Energia mês máx (kWh)",
             "Mês de maior consumo", "Demanda pico estimada (kW)", "Hora do pico",
             "Carregamento estimado (%)", "Situação carregamento", "Carregamento do alimentador (%)",
             "GD no alimentador (kW)", "Carregamento de GD do alimentador (%)",
             "Capacidade disponível p/ GD no alimentador (kW)",
             "Carga mín. diurna estimada (kW)",
             "Mês de menor consumo", "Ocupação GD / carga mín. (%)", "Margem p/ nova GD sem inversão (kW)",
             "Situação inversão de fluxo", "Capacidade disponível estimada (kW)", "UCs sem curva típica", "POT_NOM (bruto)", "lat", "lon",
             "Google Maps"]
    df = df[[c for c in ordem if c in df.columns]].sort_values(
        "GD / potência do trafo (%)", ascending=False)

    # Alimentadores
    alim = df.groupby("Alimentador (CTMT)", dropna=False).agg(**{
        "Nº trafos": ("Transformador (COD_ID)", "size"),
        "Potência instalada trafos (kVA)": ("Potência nominal (kVA)", "sum"),
        "Nº UCs": ("Nº UCs", "sum"),
        "Nº UGs (GD)": ("Nº UGs (GD)", "sum"),
        "GD instalada (kW)": ("GD instalada (kW)", "sum"),
        "Energia média mensal (kWh)": ("Energia média mensal (kWh)", "sum"),
        "Trafos em risco de inversão": ("Situação inversão de fluxo", lambda s: (s == "risco").sum()),
        "Trafos em sobrecarga": ("Situação carregamento", lambda s: (s == "sobrecarga").sum()),
    }).reset_index()
    alim["GD / kVA trafos (%)"] = alim["GD instalada (kW)"] / alim["Potência instalada trafos (kVA)"] * 100
    if len(tab_ct):
        alim = alim.merge(tab_ct, on="Alimentador (CTMT)", how="left")
    alim = alim.sort_values("GD instalada (kW)", ascending=False)

    # Municípios
    conta = lambda v: (lambda s: (s == v).sum())
    tab_mun = df.groupby(["Município (IBGE)", "Município"]).agg(**{
        "Nº trafos": ("Transformador (COD_ID)", "size"),
        "Potência instalada trafos (kVA)": ("Potência nominal (kVA)", "sum"),
        "Nº UCs": ("Nº UCs", "sum"),
        "Nº UGs (GD)": ("Nº UGs (GD)", "sum"),
        "GD instalada (kW)": ("GD instalada (kW)", "sum"),
        "Capacidade disponível estimada (kW)": ("Capacidade disponível estimada (kW)", "sum"),
        "Trafos com capacidade ≥ 10 kW": ("Capacidade disponível estimada (kW)", lambda s: (s >= 10).sum()),
        "Trafos — risco de inversão": ("Situação inversão de fluxo", conta("risco")),
        "Trafos — atenção": ("Situação inversão de fluxo", conta("atenção")),
        "Trafos — folga": ("Situação inversão de fluxo", conta("folga")),
        "Trafos — sem GD": ("Situação inversão de fluxo", conta("sem GD")),
        "Trafos em sobrecarga": ("Situação carregamento", conta("sobrecarga")),
    }).reset_index()
    tab_mun["GD / kVA trafos (%)"] = tab_mun["GD instalada (kW)"] / tab_mun["Potência instalada trafos (kVA)"] * 100
    tab_mun = tab_mun.sort_values("GD instalada (kW)", ascending=False)

    # Resumo
    cont_inv = df["Situação inversão de fluxo"].value_counts()
    cont_car = df["Situação carregamento"].value_counts()
    resumo = pd.DataFrame([
        ("Arquivo BDGD", nome_gdb),
        ("Município (IBGE)", f"todos ({len(tab_mun)} municípios)" if todos else a.mun),
        ("Transformadores analisados", len(df)),
        ("Capacidade disponível estimada total (kW)", round(df["Capacidade disponível estimada (kW)"].sum(), 1)),
        ("Trafos com capacidade ≥ 10 kW", int((df["Capacidade disponível estimada (kW)"] >= 10).sum())),
        ("UCs de BT ativas", int(df["Nº UCs"].sum())),
        ("Unidades geradoras (GD BT)", int(df["Nº UGs (GD)"].sum())),
        ("GD instalada total (kW)", round(df["GD instalada (kW)"].sum(), 4)),
        ("Potência total dos trafos (kVA)", round(df["Potência nominal (kVA)"].sum(), 4)),
        ("Trafos — risco de inversão", int(cont_inv.get("risco", 0))),
        ("Trafos — atenção", int(cont_inv.get("atenção", 0))),
        ("Trafos — folga", int(cont_inv.get("folga", 0))),
        ("Trafos — sem GD", int(cont_inv.get("sem GD", 0))),
        ("Trafos — sem dado de carga", int(cont_inv.get("sem dado", 0))),
        ("Trafos — sobrecarga estimada (>100%)", int(cont_car.get("sobrecarga", 0))),
        ("Trafos — carregamento alto (80–100%)", int(cont_car.get("alto", 0))),
        ("Fator de potência usado", a.fp),
        ("Janela solar (h)", f"{a.janela[0]:g}–{a.janela[1]:g}"),
        ("Fator de segurança", a.fator_seg),
        ("Limiar de atenção (%)", a.atencao),
        ("POT_NOM em valores comerciais de kVA (%)", round(pct_padrao, 1)),
        ("Valores de POT_INST corrigidos de W → kW", corrigidas),
        ("POT_INST = energia máx. mensal ÷ 720 h (%)", round(pct_energia, 1)),
        ("Fonte da potência da GD", fonte_gd),
        ("Gerado em", time.strftime("%d/%m/%Y %H:%M")),
    ], columns=["Item", "Valor"])

    leiame = pd.DataFrame({"Método e ressalvas": [
        "FONTE: BDGD/ANEEL (dados abertos). Retrata a rede na data-base do arquivo, não hoje.",
        "LIGAÇÃO: UCs (UCBT), GD (UGBT) e iluminação pública (PIP) são ligadas ao trafo pelo campo UNI_TR_MT (ou UNI_TR_D).",
        "GD INSTALADA: soma da potência instalada das unidades geradoras de BT ativas no trafo. Com --gd-aneel, vem da base oficial de GD da ANEEL (MdaPotenciaInstaladaKW, cruzada pelo CEG_GD); sem ela, do POT_INST da BDGD (valores > 1000 tratados como W).",
        "DEMANDA: energia mensal de cada UC (ENE_01..12) → kW médio → multiplicada pela curva típica da UC (CRVCRG via TIP_CC).",
        "ALIMENTADOR: demanda de pico = energia injetada no mês de maior energia (CTMT) em kW médio × fator de pico da soma das curvas típicas das cargas do alimentador; capacidade = √3 × tensão nominal (TEN_NOM) × corrente nominal (SEGCON.CNOM) do condutor do trecho de saída; carregamento = pico ÷ fp ÷ capacidade. Não considera limites de proteção/disjuntor nem manobras.",
        "GD NO ALIMENTADOR: soma das UGs de BT (UGBT) e MT (UGMT) ativas no alimentador; potência pela base de GD da ANEEL (CEG_GD); sem casamento, MT usa DEM_CONT (usinas como PCH/UHE/UTE). CARREGAMENTO DE GD (%) = GD ÷ capacidade do alimentador. CARGA MÍN. DO ALIMENTADOR = (energia medida no alimentador, que já vem líquida da GD, + geração estimada da GD com perfil FV típico do RS de ~1.150 kWh/kWp·ano) no mês de menor carga, × razão mínimo-na-janela-solar/média das curvas típicas das cargas. CAPACIDADE P/ GD NO ALIMENTADOR = menor entre (carga mín. × fator de segurança − GD) e (capacidade − GD).",
        "PICO: maior valor da soma das curvas no mês de maior consumo do trafo, entre dia útil, sábado e domingo.",
        "CARREGAMENTO (%) = pico estimado (kW) ÷ fp ÷ potência nominal (kVA) × 100.",
        "CARGA MÍN. DIURNA: menor valor da soma das curvas dentro da janela solar, no mês de menor consumo.",
        "OCUPAÇÃO (%) = GD instalada ÷ (carga mín. diurna × fator de segurança) × 100.",
        "SITUAÇÃO: ≥100% risco · ≥ limiar atenção · abaixo folga · sem carga = 'sem dado' (nunca 'folga').",
        "LIMITE: a energia das UCs com GD na BDGD é a medida da rede; o autoconsumo não aparece, então a carga dessas UCs fica subestimada.",
        "LIMITE: UCs de outro município ligadas a um trafo de Santa Maria (divisa) não entram na carga.",
        "CONFERIR: se 'POT_NOM em valores comerciais de kVA' no Resumo for baixo, o campo pode estar codificado — confira a coluna 'POT_NOM (bruto)'.",
        "USO: estimativa de engenharia para triagem. Não é medição nem substitui a análise de inversão de fluxo da distribuidora (REN 1.000/2021).",
    ]})

    rotulo = "todos" if todos else a.mun
    xlsx = saida / f"BDGD_trafos_{rotulo}.xlsx"
    log(f"Salvando {xlsx} ...")
    abas = [("Transformadores", df), ("Municípios", tab_mun), ("Alimentadores", alim),
            ("Resumo", resumo), ("Leia-me", leiame)]
    if not todos:
        abas.pop(1)
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        for nome, tab in abas:
            grande = len(tab) > 50000          # formatar célula a célula fica lento demais
            if grande:
                tab = tab.round({c: 4 for c in tab.columns
                                 if pd.api.types.is_float_dtype(tab[c]) and c not in ("lat", "lon")})
            tab.to_excel(w, sheet_name=nome, index=False)
            ws = w.sheets[nome]
            ws.freeze_panes = "B2"
            for j, col in enumerate(tab.columns, start=1):
                letra = ws.cell(1, j).column_letter
                larg = max(len(str(col)), *(len(str(v)) for v in tab[col].head(200))) if len(tab) else len(col)
                ws.column_dimensions[letra].width = min(60 if nome == "Leia-me" else 34, larg + 2)
                if not grande and pd.api.types.is_float_dtype(tab[col]) and col not in ("lat", "lon"):
                    for cel in ws.iter_rows(min_row=2, min_col=j, max_col=j):
                        cel[0].number_format = "#,##0.0000"
            if nome == "Leia-me":
                ws.column_dimensions["A"].width = 140
            ws.auto_filter.ref = ws.dimensions

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", nome_gdb)
    base = f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else nome_gdb
    meta = {"base": base, "fs": a.fator_seg, "fp": a.fp}
    if todos:
        site = saida / "site_todos"
        meta.update(munNome="Rio Grande do Sul", titulo=a.mun_nome or "Área de concessão")
        n_mun = salvar_site(df, site, meta)
        log("Gerando rede BT e postes com GD por trafo ...")
        log(f"  rede BT de {salvar_redes_bt(gdb, L, site, df, ug):,} trafos em {site / 'dados' / 'bt'}")
        log(f"  traçado de {salvar_redes_mt(gdb, L, site):,} alimentadores em {site / 'dados' / 'mt'}")
        log(f"Site com {n_mun} municípios salvo em {site} (publique o conteúdo da pasta)")
    else:
        nm, uf = nomes.get(mun.lstrip("0"), (mun, ""))
        nome_mun = a.mun_nome or (f"{nm}, {uf}" if uf else nm)
        html = saida / f"Capacidade_Hospedagem_{a.mun}.html"
        salvar_plataforma(df, html, dict(meta, munNome=nome_mun))
        log(f"Plataforma de consulta salva em {html}")
    log(f"Pronto: {len(df):,} trafos | GD {df['GD instalada (kW)'].sum():,.1f} kW | "
        f"risco {cont_inv.get('risco', 0)} | atenção {cont_inv.get('atenção', 0)} | "
        f"sobrecarga {cont_car.get('sobrecarga', 0)}")


TEMPLATE = r"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Capacidade de Hospedagem __MUNNOME__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@600;700&family=JetBrains+Mono:wght@400;600&display=swap">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
/* Layout: mapa em tela cheia; painel lateral (desktop) ou gaveta inferior (celular) */
:root{
  --bg:#f3f5f4; --panel:#ffffff; --ink:#15211d; --muted:#5d6b66; --line:#d9e0dd;
  --accent:#0f6b5c; --accent-ink:#ffffff; --chip:#e8eeec;
  --ok:#1f9d55; --mid:#e0a100; --low:#ea6a1f; --none:#c62828; --nodata:#7a8784;
  --display:"Barlow Condensed","Arial Narrow",Arial,sans-serif;
  --body:"Barlow",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e1513; --panel:#16201d; --ink:#e6eeeb; --muted:#97a7a2; --line:#2a3733;
  --accent:#4fc3a8; --accent-ink:#06261f; --chip:#22302c; --nodata:#8a9894; color-scheme:dark}}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.45 var(--body);display:grid;grid-template-columns:400px minmax(0,1fr);overflow:hidden}
#map{height:100%;width:100%;background:var(--bg)}
aside{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0;height:100%;overflow-y:auto;scrollbar-gutter:stable;scrollbar-width:thin}
header{padding:16px 18px 12px;border-bottom:1px solid var(--line);display:grid;gap:10px}
.brand{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
h1{font:700 22px/1.1 var(--display);letter-spacing:.02em;text-transform:uppercase;margin:0;text-wrap:balance}
.base{font:12px var(--mono);color:var(--muted)}
.search{display:flex;gap:6px}
.search input{flex:1;min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
button{font:600 14px var(--body);border:0;border-radius:8px;padding:10px 14px;background:var(--accent);color:var(--accent-ink);cursor:pointer}
button.ghost{background:var(--chip);color:var(--ink)}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.hint{font-size:12px;color:var(--muted);margin:0}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.row label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.row input,.row select{padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit;min-width:0}
#pot{width:90px;font-family:var(--mono)}
#alim,#mun{flex:1}
details.menu{border:1px solid var(--line);border-radius:8px;padding:8px 10px;display:grid;gap:8px}
details.menu[open]{gap:8px}
details.menu summary{cursor:pointer;font:700 14px var(--display);text-transform:uppercase;letter-spacing:.06em;color:var(--accent)}
details.menu > *:not(summary){margin-top:8px}
details.menu summary .base{text-transform:none;letter-spacing:0;font-weight:400}
#empKwh{width:90px;font-family:var(--mono)}
header,aside,details.menu{min-width:0}
.row select{max-width:100%}#empAtv,#alim,#mun,#empTen,#empGd,#empRede{flex:1 1 0;width:0;min-width:110px}
#kvaMin,#kvaMax{flex:1;font-family:var(--mono)}
.chk{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--muted)}
.chk[hidden]{display:none}
.chk input{width:18px;height:18px;accent-color:var(--accent)}
.scroll{overflow:visible;flex:none;padding:14px 18px 24px;display:grid;gap:16px;align-content:start}
aside::-webkit-scrollbar{width:10px}aside::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px}
.leaflet-tooltip{font:13px/1.4 var(--body)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.stat{background:var(--chip);border-radius:8px;padding:8px 10px}
.stat b{display:block;font:600 17px var(--mono);font-variant-numeric:tabular-nums}
.stat span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.legend{display:grid;gap:4px;font-size:13px}
.legend div{display:flex;align-items:center;gap:8px}
.dot{width:12px;height:12px;border-radius:50%;flex:none;border:1.5px solid rgba(0,0,0,.25)}
h2{font:700 15px var(--display);text-transform:uppercase;letter-spacing:.08em;margin:0 0 6px;color:var(--muted)}
.card{border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;gap:8px}
.card .top{display:flex;justify-content:space-between;gap:8px;align-items:center}
.card .id{font:600 15px var(--mono);word-break:break-all}
.pill{font:600 12px var(--body);padding:3px 9px;border-radius:999px;color:#fff;white-space:nowrap}
dl{display:grid;grid-template-columns:1fr auto;gap:3px 12px;margin:0;font-size:13.5px}
dt{color:var(--muted)} dd{margin:0;font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
.big{font:600 26px var(--mono);font-variant-numeric:tabular-nums}
.big small{font-size:14px;color:var(--muted)}
.near{display:grid;gap:6px}
.near button{display:grid;grid-template-columns:14px 1fr auto;gap:10px;align-items:center;text-align:left;background:var(--chip);color:var(--ink);font-weight:500;padding:9px 10px}
.near .d{font:12px var(--mono);color:var(--muted)}
.links{display:flex;gap:10px;flex-wrap:wrap;font-size:13px}
.links a{color:var(--accent)}
.note{font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:10px}
.msg{font-size:13px;color:var(--low)}
.toggle{display:none}
.leaflet-container{font:inherit}
@media (max-width:760px){
  body{grid-template-columns:1fr;grid-template-rows:1fr auto}
  #map{grid-row:1}
  body{grid-template-columns:minmax(0,1fr)}
  aside{grid-row:2;height:auto;max-height:58vh;border-right:0;border-top:1px solid var(--line);
        padding-bottom:env(safe-area-inset-bottom,0px);overflow-y:auto}
  aside .scroll{overflow:visible;flex:none}
  aside.min{overflow:hidden}aside.min header>*:not(.brand){display:none}
  aside.min .scroll{display:none}
  .toggle{display:inline-block}
  header{padding:12px max(16px,env(safe-area-inset-left,0px)) 10px max(16px,env(safe-area-inset-right,0px))}
  .scroll{padding-left:16px;padding-right:16px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<aside id="side">
  <header>
    <div class="brand">
      <h1>Capacidade de Hospedagem · <span id="titulo">__MUNNOME__</span></h1>
      <button class="ghost toggle" id="tog" aria-expanded="true">Painel</button>
    </div>
    <div class="base">BDGD __BASE__</div>
    <form class="search" id="f">
      <input id="q" type="search" placeholder="Endereço, coordenada, trafo ou alimentador" autocomplete="off">
      <button type="submit">Buscar</button>
    </form>
    <p class="hint">Ou clique no mapa para ver os transformadores mais próximos do ponto.</p>
    <div class="row" id="munRow" style="display:none">
      <label for="mun">Município</label>
      <select id="mun"></select>
    </div>
    <details id="ftr" class="menu" open>
    <summary>Filtros de transformadores <span class="base" id="ftrN"></span></summary>
    <div class="row">
      <label for="pot">GD pretendida</label>
      <input id="pot" type="number" min="0" step="0.1" placeholder="kW">
      <label for="alim">Alimentador</label>
      <select id="alim"><option value="">Todos</option></select>
    </div>
    <div class="row">
      <label for="kvaMin">Trafo de</label>
      <select id="kvaMin"><option value="">mín.</option></select>
      <label for="kvaMax">até</label>
      <select id="kvaMax"><option value="">máx.</option></select>
      <span class="base">kVA</span>
    </div>
    <label class="chk"><input type="checkbox" id="uc1"> Só trafos com 1 unidade consumidora</label>
    <label class="chk"><input type="checkbox" id="alimOk"> Só trafos cujo alimentador comporta a GD pretendida</label>
    <label class="chk" id="filtraRow" hidden><input type="checkbox" id="filtra" checked> Mostrar só os trafos que comportam a GD pretendida</label>
    </details>
    <details id="prosp" class="menu" hidden>
      <summary>Prospecção de empresas</summary>
      <label class="chk"><input type="checkbox" id="empOn"> Mostrar empresas no mapa</label>
      <label class="chk"><input type="checkbox" id="empHideTr"> Esconder os trafos do mapa</label>
      <div class="row"><label for="empAtv">Atividade</label><select id="empAtv"><option value="">Todas</option></select></div>
      <div class="row"><label for="empKwh">Consumo mín.</label><input id="empKwh" type="number" min="0" step="100" value="1000"><span class="base">kWh/mês</span>
        <label for="empTen">Rede</label><select id="empTen"><option value="">BT e MT</option><option value="0">BT</option><option value="1">MT</option></select></div>
      <div class="row"><label for="empGd">GD</label><select id="empGd"><option value="0">Sem GD</option><option value="1">Sem GD ou talvez</option><option value="">Todas</option></select>
        <label for="empRede">Rede comporta</label><select id="empRede"><option value="">Qualquer</option><option value="3">Sim</option><option value="2">Sim ou parcial</option></select></div>
      <button type="button" class="ghost" id="empCsv">Baixar lista (CSV)</button>
    </details>
  </header>
  <div class="scroll" id="info"></div>
</aside>
<div id="map"></div>
<script>
let D=__DATA__||[];
const META=__META__;
const COR={ok:"var(--ok)",mid:"var(--mid)",low:"var(--low)",none:"var(--none)",nd:"var(--nodata)"};
const HEX={};function cssVar(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}
function hex(k){return cssVar(COR[k].slice(4,-1))}
const fmt=(v,d=1)=>v==null||isNaN(v)?"–":Number(v).toLocaleString("pt-BR",{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// capacidade disponível estimada = menor entre (carga mín. diurna × fs − GD) e (kVA − GD)
const kvaMinEl=document.getElementById("kvaMin"),kvaMaxEl=document.getElementById("kvaMax");
const fmtK=v=>fmt(v,v%1?1:0);
function preparar(){D.forEach(t=>{
  const inv=t.cmin==null?null:t.cmin*META.fs-t.gd;
  const lim=t.kva==null?null:t.kva-t.gd;
  t.capt=(inv==null&&lim==null)?null:Math.max(0,Math.min(inv??Infinity,lim??Infinity));
  t.limt=inv==null?"sem dado":(lim!=null&&lim<inv?"potência do trafo":"inversão de fluxo");
  t.cap=t.capt;t.lim=t.limt;
});}
preparar();
function classe(t,pot){
  if(t.cap==null) return "nd";
  if(pot>0) return t.cap>=pot?"ok":(t.cap>0?"low":"none");
  return t.cap<=0?"none":t.cap<10?"low":t.cap<30?"mid":"ok";
}
const ROT={ok:"capacidade ≥ 30 kW",mid:"10 a 30 kW",low:"até 10 kW",none:"sem capacidade",nd:"sem dado de carga"};
const ROTP={ok:"comporta a GD pretendida",low:"capacidade insuficiente",none:"sem capacidade",nd:"sem dado de carga"};

const map=L.map("map",{preferCanvas:true,zoomControl:true});
const osm=L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:20,maxNativeZoom:19,attribution:"© OpenStreetMap · BDGD/ANEEL"});
const sat=L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{maxZoom:20,maxNativeZoom:19,attribution:"Imagens © Esri · BDGD/ANEEL"});
osm.addTo(map);L.control.layers({"Mapa":osm,"Satélite":sat},null,{position:"topright"}).addTo(map);

const capa=L.layerGroup().addTo(map);let marcas=new Map(),pino=null,sel=null;
// Rede BT do trafo selecionado + postes com GD (dados/bt/<alimentador>.json, sob demanda)
const rede=L.layerGroup().addTo(map),btCache={};
// Ao clicar numa empresa: BT → destaca o trafo e desenha a rede BT dele; MT → desenha o alimentador inteiro
const redeMt=L.layerGroup().addTo(map),mtCache={};
map.createPane("topo");map.getPane("topo").style.zIndex=450;map.getPane("topo").style.pointerEvents="none";
const rTopo=L.canvas({pane:"topo"});
function linhas(d,estilo,grupo){const ls=[];d.l.forEach(a=>{let x=a[0],y=a[1];const pts=[[y/1e5,x/1e5]];
  for(let i=2;i<a.length;i+=2){x+=a[i];y+=a[i+1];pts.push([y/1e5,x/1e5])}ls.push(L.polyline(pts,estilo).addTo(grupo))});return ls}
async function mostrarAlim(x){
  const al=x[14];if(!al)return;
  const f=String(al).replace(/[^\w.-]/g,"_");
  try{if(!(f in mtCache)){const r=await fetch(`dados/mt/${f}.json`);mtCache[f]=r.ok?await r.json():null}}catch(e){mtCache[f]=null}
  if(empSel!==x)return;
  const d=mtCache[f];x._km=d?d.km:0;
  if(d){const ls=linhas(d,{color:"#1f5fbf",weight:4,opacity:.9,interactive:false,renderer:rTopo},redeMt);
    if(ls.length)map.fitBounds(L.featureGroup(ls).getBounds(),{padding:[20,20]})}
  render();
}
function selEmp(x){
  empSel=x;redeMt.clearLayers();rede.clearLayers();ponto=null;if(pino){map.removeLayer(pino);pino=null}
  if(x[11]){sel=null;desenhar();render();mostrarAlim(x)}
  else{
    const t=D.find(t=>t.id===x[13]);sel=t||null;desenhar();render();
    if(t){mostrarRede(t);
      L.circleMarker([t.lat,t.lon],{radius:11,color:cssVar("--ink"),weight:3,fillColor:hex(classe(t,+potEl.value||0)),fillOpacity:1})
        .bindTooltip(()=>dicaTrafo(t),{direction:"top",offset:[0,-8]}).addTo(redeMt);
      L.polyline([[x[0],x[1]],[t.lat,t.lon]],{color:"#8e44ec",weight:2.5,dashArray:"5 5",interactive:false,renderer:rTopo}).addTo(redeMt);
      map.fitBounds(L.latLngBounds([[x[0],x[1]],[t.lat,t.lon]]).pad(.6),{maxZoom:18})}
  }
  abrirPainel();topoPainel();
}
async function mostrarRede(t){
  rede.clearLayers();
  if(!t||!META.muns||!t.alim)return;
  const f=String(t.alim).replace(/[^\w.-]/g,"_");
  try{if(!(f in btCache)){const r=await fetch(`dados/bt/${f}.json`);btCache[f]=r.ok?await r.json():{}}}catch(e){return}
  const d=btCache[f][t.id];if(sel!==t)return;
  t.bt=d?{km:d.km,np:d.g.length,kw:d.g.reduce((s,p)=>s+p[2],0)}:{km:0,np:0,kw:0};
  if(d){
    const cor=cssVar("--accent");
    d.l.forEach(a=>{let x=a[0],y=a[1];const pts=[[y/1e5,x/1e5]];for(let i=2;i<a.length;i+=2){x+=a[i];y+=a[i+1];pts.push([y/1e5,x/1e5])}
      L.polyline(pts,{color:cor,weight:4,opacity:.8,interactive:false}).addTo(rede)});
    d.g.forEach(p=>L.circleMarker([p[0],p[1]],{radius:7,color:"#5a2d00",weight:2,fillColor:"#ffcc00",fillOpacity:1})
      .bindTooltip(`GD ${fmt(p[2])} kW · ${p[3]} UG${p[3]>1?"s":""} neste poste`).addTo(rede));
  }
  render();
}
const potEl=document.getElementById("pot"),alimEl=document.getElementById("alim"),info=document.getElementById("info");
// faixas de potência (kVA) presentes no município; mantém a escolha se o valor existir
function preencherKva(){
  const vs=[...new Set(D.map(t=>t.kva).filter(v=>v!=null))].sort((a,b)=>a-b);
  for(const el of [kvaMinEl,kvaMaxEl]){const ant=el.value;el.length=1;
    vs.forEach(v=>{const o=document.createElement("option");o.value=v;o.textContent=fmtK(v);el.append(o)});
    el.value=vs.includes(+ant)?ant:""}
}
function preencherAlim(){preencherKva();alimEl.length=1;[...new Set(D.map(t=>t.alim).filter(Boolean))].sort().forEach(a=>{const o=document.createElement("option");o.value=o.textContent=a;alimEl.append(o)})}
preencherAlim();

// base(): filtro de alimentador; visiveis(): base + (se marcado) só trafos com capacidade ≥ GD pretendida
// alimentador "comporta": capacidade p/ nova GD do alimentador ≥ GD pretendida (sem GD informada: > 0)
function alimComporta(t){const pot=+potEl.value||0;return t.capa!=null&&(pot>0?t.capa>=pot:t.capa>0)}
function base(){
  const a=alimEl.value,u=uc1El.checked,ao=document.getElementById("alimOk").checked,k0=kvaMinEl.value===""?null:+kvaMinEl.value,k1=kvaMaxEl.value===""?null:+kvaMaxEl.value;
  if(!a&&!u&&!ao&&k0==null&&k1==null)return D;
  return D.filter(t=>(!a||t.alim===a)&&(!u||t.ucs===1)&&(!ao||alimComporta(t))&&(k0==null||(t.kva!=null&&t.kva>=k0))&&(k1==null||(t.kva!=null&&t.kva<=k1)));
}
function filtrando(){return (+potEl.value||0)>0&&filtraEl.checked}
function visiveis(){const v=base(),pot=+potEl.value||0;return filtrando()?v.filter(t=>t.cap!=null&&t.cap>=pot):v}
function desenhar(){
  if(!sel)rede.clearLayers();
  if(!empSel)redeMt.clearLayers();
  capa.clearLayers();marcas.clear();const pot=+potEl.value||0;
  visiveis().forEach(t=>{
    const k=classe(t,pot),c=hex(k);
    const m=L.circleMarker([t.lat,t.lon],{radius:t===sel?9:5,color:t===sel?cssVar("--ink"):c,weight:t===sel?3:1,fillColor:c,fillOpacity:.85});
    m.bindTooltip(()=>dicaTrafo(t),{direction:"top",offset:[0,-6]});
    m.on("click",e=>{L.DomEvent.stopPropagation(e);abrir(t)});capa.addLayer(m);marcas.set(t.id,m);
  });
}
function resumo(){
  const v=base(),pot=+potEl.value||0;const n={ok:0,mid:0,low:0,none:0,nd:0};v.forEach(t=>n[classe(t,pot)]++);
  const oc=filtrando()?v.length-visiveis().length:0;
  const ca=alimEl.value&&v.length?v[0].ca:null;
  const gd=v.reduce((s,t)=>s+t.gd,0),kva=v.reduce((s,t)=>s+(t.kva||0),0);
  const r=pot>0?ROTP:ROT;
  return `<section><h2>${alimEl.value?"Alimentador "+esc(alimEl.value):(META.muns?esc(META.munNome.split(",")[0]):"Município")}</h2>
  <div class="stats"><div class="stat"><b>${v.length.toLocaleString("pt-BR")}</b><span>trafos</span></div>
  <div class="stat"><b>${fmt(gd/1000,2)}</b><span>MW de GD</span></div>
  <div class="stat"><b>${fmt(kva/1000,2)}</b><span>MVA instalado</span></div></div>
  ${alimEl.value&&v.length?`<dl><dt>Carregamento do alimentador</dt><dd>${fmt(ca)} %</dd><dt>GD no alimentador</dt><dd>${fmt(v[0].gda,0)} kW</dd>
  <dt>Carregamento de GD do alimentador</dt><dd>${fmt(v[0].cga)} %</dd><dt>Capacidade p/ nova GD no alimentador</dt><dd>${fmt(v[0].capa,0)} kW</dd></dl>`:""}
  ${uc1El.checked?`<p class="hint">Mostrando só trafos com 1 unidade consumidora.</p>`:""}
  ${document.getElementById("alimOk").checked?`<p class="hint">Mostrando só trafos cujo alimentador comporta ${(+potEl.value||0)>0?fmt(+potEl.value)+" kW":"nova GD"}.</p>`:""}
  ${kvaMinEl.value||kvaMaxEl.value?`<p class="hint">Trafos de ${kvaMinEl.value?fmtK(+kvaMinEl.value):"qualquer"} a ${kvaMaxEl.value?fmtK(+kvaMaxEl.value):"qualquer"} kVA.</p>`:""}</section>
  <section><h2>${pot>0?"Para "+fmt(pot)+" kW":"Capacidade disponível"}</h2><div class="legend">
  ${Object.keys(r).map(k=>`<div><span class="dot" style="background:${COR[k]}"></span>${r[k]} <span class="base">(${n[k]})</span></div>`).join("")}</div>
  ${oc?`<p class="hint">${oc.toLocaleString("pt-BR")} trafos com capacidade abaixo de ${fmt(pot)} kW estão ocultos no mapa.</p>`:""}</section>`;
}
function rodape(){return `<p class="note">Estimativa feita a partir da BDGD/ANEEL (data-base ${esc(META.base)}): carga das UCs pela energia mensal e curvas típicas, GD pela potência instalada cadastrada. Capacidade = menor valor entre a carga mínima diurna (×${META.fs}) e a potência do trafo, descontada a GD existente. Capacidade do alimentador p/ nova GD = menor valor entre a carga mínima diurna do alimentador e a capacidade do condutor de saída, descontada a GD em BT e MT. Serve para triagem; a viabilidade de conexão é definida pela distribuidora.</p>`}
function ficha(t){
  const pot=+potEl.value||0,k=classe(t,pot),r=pot>0?ROTP:ROT;
  return `<div class="card"><div class="top"><span class="id">${esc(t.id)}</span><span class="pill" style="background:${COR[k]}">${r[k]}</span></div>
  <div><div class="base">Capacidade disponível estimada</div><div class="big">${fmt(t.cap)} <small>kW</small></div><div class="base">limitada por: ${esc(t.lim)}</div></div>
  <dl><dt>Alimentador</dt><dd>${esc(t.alim||"–")}</dd><dt>Subestação</dt><dd>${esc(t.sub||"–")}</dd>
  <dt>Potência do trafo</dt><dd>${fmt(t.kva,1)} kVA</dd><dt>GD instalada</dt><dd>${fmt(t.gd,2)} kW</dd>
  <dt>Unidades geradoras</dt><dd>${t.ugs}</dd><dt>Unidades consumidoras</dt><dd>${t.ucs}</dd>
  <dt>Carga mín. diurna</dt><dd>${fmt(t.cmin,2)} kW</dd><dt>Demanda de pico</dt><dd>${fmt(t.pico,2)} kW</dd>
  <dt>Carregamento do trafo</dt><dd>${fmt(t.car)} %</dd><dt>GD / trafo</dt><dd>${fmt(t.kva?t.gd/t.kva*100:null)} %</dd>
</dl>
  <div class="base">Alimentador ${esc(t.alim||"–")}</div>
  <dl><dt>Carregamento do alimentador</dt><dd>${fmt(t.ca)} %</dd><dt>GD no alimentador</dt><dd>${fmt(t.gda,0)} kW</dd>
  <dt>Carregamento de GD do alimentador</dt><dd>${fmt(t.cga)} %</dd><dt>Capacidade p/ nova GD no alimentador</dt><dd>${fmt(t.capa,0)} kW</dd></dl>
  ${t.bt&&!t.bt.km&&!t.bt.np?`<div class="base">Sem rede BT nem GD cadastradas para este trafo na BDGD.</div>`:t.bt?`<div class="base">Rede BT no mapa: ${fmt(t.bt.km,2)} km · ${t.bt.np} poste${t.bt.np===1?"":"s"} com GD (${fmt(t.bt.kw)} kW) <span class="dot" style="display:inline-block;vertical-align:middle;background:#ffcc00"></span></div>`:(META.muns?`<div class="base">Carregando rede BT…</div>`:"")}
  <div class="links"><a href="https://www.google.com/maps?q=${t.lat},${t.lon}" target="_blank" rel="noopener">Abrir no Google Maps</a>
  <span class="base">${t.lat.toFixed(6)}, ${t.lon.toFixed(6)}</span></div></div>`;
}
function dist(a,b,c,d){const R=6371e3,r=Math.PI/180,x=(d-b)*r*Math.cos((a+c)/2*r),y=(c-a)*r;return Math.sqrt(x*x+y*y)}
function proximos(lat,lon,n=6){return visiveis().map(t=>({t,d:dist(lat,lon,t.lat,t.lon)})).sort((a,b)=>a.d-b.d).slice(0,n)}
function listaProx(lat,lon){
  const pot=+potEl.value||0;
  return `<section><h2>Trafos mais próximos do ponto</h2><div class="near">${proximos(lat,lon).map(({t,d})=>
   `<button data-id="${esc(t.id)}"><span class="dot" style="background:${COR[classe(t,pot)]}"></span><span><b class="id">${esc(t.id)}</b><br><span class="d">${fmt(t.kva,1)} kVA · GD ${fmt(t.gd,1)} kW</span></span><span><b>${fmt(t.cap)}</b> <span class="d">kW</span><br><span class="d">${d<1000?Math.round(d)+" m":fmt(d/1000,2)+" km"}</span></span></button>`).join("")}</div>
   <p class="hint">${filtrando()?"Só trafos que comportam "+fmt(+potEl.value)+" kW. ":""}Distância em linha reta. A conexão segue a rede de BT do trafo que atende o local.</p></section>`;
}
let ponto=null,msg="";
function render(){
  let h="";if(msg)h+=`<p class="msg">${esc(msg)}</p>`;
  if(empSel)h+=fichaEmp(empSel);
  if(sel)h+=`<section><h2>Transformador</h2>${ficha(sel)}</section>`;
  if(ponto)h+=listaProx(ponto[0],ponto[1]);
  h+=resumoEmp()+resumo()+rodape();info.innerHTML=h;
  info.querySelectorAll("button[data-tr]").forEach(b=>b.onclick=()=>{const t=D.find(x=>x.id===b.dataset.tr);if(t){empSel=null;abrir(t)}});
  info.querySelectorAll(".near button").forEach(b=>b.onclick=()=>abrir(D.find(t=>t.id===b.dataset.id)));
}
function abrir(t,voar=true){sel=t;msg="";empSel=null;desenhar();render();mostrarRede(t);if(voar)map.setView([t.lat,t.lon],Math.max(map.getZoom(),17));topoPainel();abrirPainel()}
function marcarPonto(lat,lon,rotulo){
  ponto=[lat,lon];empSel=null;if(pino)map.removeLayer(pino);
  pino=L.marker([lat,lon]).addTo(map).bindTooltip(rotulo||"Ponto consultado");
  const p=proximos(lat,lon,1)[0];sel=p?p.t:null;msg="";desenhar();render();mostrarRede(sel);map.setView([lat,lon],18);abrirPainel();
}
map.on("click",e=>pontoGlobal(e.latlng.lat,e.latlng.lng));
// Área inteira: troca de município sob demanda (dados/<código>.json)
function dentro(b,lat,lon){return b&&lat>=b[0]&&lat<=b[2]&&lon>=b[1]&&lon<=b[3]}
function distC(b,lat,lon){return (lat-(b[0]+b[2])/2)**2+(lon-(b[1]+b[3])/2)**2}
async function pontoGlobal(lat,lon,rot){
  if(META.muns&&!dentro(META.bbox,lat,lon)){
    const m=META.muns.filter(x=>dentro(x.b,lat,lon)).sort((x,y)=>distC(x.b,lat,lon)-distC(y.b,lat,lon))[0];
    if(m&&m.c!==META.mun)await carregarMun(m.c,false);
  }
  marcarPonto(lat,lon,rot);
}
async function carregarMun(c,aj=true){
  const m=META.muns.find(x=>x.c===c)||META.muns[0];
  msg="Carregando "+m.n+"…";render();
  try{const r=await fetch(`dados/${m.c}.json`);if(!r.ok)throw 0;D=await r.json()}
  catch(e){msg="Não consegui carregar os dados de "+m.n+".";render();return}
  META.mun=m.c;META.bbox=m.b;META.munNome=m.n+", RS";munEl.value=m.c;
  document.getElementById("titulo").textContent=m.n;document.title="Capacidade de Hospedagem "+m.n;
  sel=null;ponto=null;msg="";if(pino){map.removeLayer(pino);pino=null}
  empSel=null;preparar();preencherAlim();desenhar();render();if(aj)ajustar();desenharEmp();
  try{history.replaceState(null,"","#"+m.c)}catch(e){}
}
// ---- Prospecção de empresas (dados/emp/<município>.json, sob demanda)
// linha: [lat,lon,kWh/mês,kWp,divisão,classe,gd,rede,endereço,bairro,grupo,mt,DIC,trafo,alimentador,demanda,CNAE]
const empLayer=L.layerGroup().addTo(map),empCache={};let empSel=null,empVis=[];
const GDR=["sem GD","talvez (GD no mesmo ponto de conexão)","provável","sim"],RED=["sem dado","não (precisa de estudo)","parcial","sim"];
const $=id=>document.getElementById(id);
async function dadosEmp(){
  const c=META.mun;if(!c)return null;
  if(!(c in empCache)){try{const r=await fetch(`dados/emp/${c}.json`);empCache[c]=r.ok?await r.json():{a:{},e:[]}}catch(e){empCache[c]={a:{},e:[]}}}
  return empCache[c];
}
function empFiltro(d){
  const atv=$("empAtv").value,kmin=+$("empKwh").value||0,ten=$("empTen").value,gd=$("empGd").value,rd=$("empRede").value;
  return d.e.filter(x=>x[2]>=kmin&&(!atv||x[4]===atv)&&(ten===""||x[11]===+ten)&&(gd===""||x[6]<=+gd)&&(rd===""||x[7]>=+rd));
}
async function desenharEmp(){
  empLayer.clearLayers();empVis=[];
  if(!META.muns||!$("empOn").checked){render();return}
  const d=await dadosEmp();if(!d)return;
  const sa=$("empAtv");
  if(sa.dataset.mun!==META.mun){const ant=sa.value;sa.length=1;
    Object.entries(d.a).sort((x,y)=>x[1].localeCompare(y[1],"pt")).forEach(([k,v])=>{const o=document.createElement("option");o.value=k;o.textContent=v;sa.append(o)});
    sa.value=ant in d.a?ant:"";sa.dataset.mun=META.mun}
  empVis=empFiltro(d);
  empVis.forEach(x=>{const m=L.circleMarker([x[0],x[1]],{radius:x[11]?8:6,color:"#3d1a78",weight:1.5,fillColor:x[6]>=2?"#b9a6d9":"#8e44ec",fillOpacity:.95});
    m.bindTooltip(()=>dicaEmp(x),{direction:"top",offset:[0,-6]});
    m.on("click",e=>{L.DomEvent.stopPropagation(e);selEmp(x)});empLayer.addLayer(m)});
  render();
}
// Informações ao passar o mouse (ou tocar) no mapa
function dicaTrafo(t){
  const pot=+potEl.value||0,k=classe(t,pot);
  return `<b>Trafo ${esc(t.id)}</b> · ${fmt(t.kva,1)} kVA<br>Capacidade p/ GD: <b>${fmt(t.cap)} kW</b> (${esc(t.lim)})<br>
  GD instalada: ${fmt(t.gd,1)} kW · ${t.ugs} UG · ${t.ucs} UCs<br>Carregamento: ${fmt(t.car)} %<br>
  Alimentador ${esc(t.alim||"–")}: cap. p/ GD ${fmt(t.capa,0)} kW · carreg. ${fmt(t.ca)} %`;
}
function dicaEmp(x){
  const a=(empCache[META.mun]||{a:{}}).a;
  return `<b>${esc(x[8]||"–")}</b> (${x[11]?"MT":"BT"})<br>${esc(x[9])} · ${esc(a[x[4]]||"")}<br>
  Consumo médio 12 meses: <b>${fmt(x[2],0)} kWh</b><br>Pg sem análise de inversão: <b>${fmt(x[17],0)} kW</b><br>
  GD: ${GDR[x[6]]} · Rede comporta: ${RED[x[7]]}`;
}
function topoPainel(){info.scrollTop=0;side.scrollTop=0}
function resumoEmp(){
  if(!META.muns||!$("empOn").checked)return "";
  const kwp=empVis.reduce((s,x)=>s+x[3],0),kwh=empVis.reduce((s,x)=>s+x[2],0);
  return `<section><h2>Prospecção</h2><div class="stats"><div class="stat"><b>${empVis.length.toLocaleString("pt-BR")}</b><span>empresas</span></div>
  <div class="stat"><b>${fmt(kwh/1000,1)}</b><span>MWh/mês</span></div><div class="stat"><b>${fmt(kwp/1000,2)}</b><span>MWp sugerido</span></div></div>
  <p class="hint">Círculos roxos = empresas (maiores = MT; claros = já têm GD provável). Toque numa empresa para ver os dados.
  Pg = consumo médio 12 meses ÷ (16% × 24 × 30) × FA da classe, arredondado para cima (REN 1.000, art. 73-A, II; Manual de Inversão de Fluxo, item 8.12). Vale para microgeração até 75 kW.</p></section>`;
}
function fichaEmp(x){
  const a=(empCache[META.mun]||{a:{}}).a,temTr=x[13]&&D.some(t=>t.id===x[13]);
  return `<section><h2>Empresa</h2><div class="card"><div class="top"><span class="id">${esc(x[8]||"–")}</span><span class="pill" style="background:#8e44ec">${x[11]?"MT":"BT"}</span></div>
  <div class="base">${esc(x[9])} · ${esc(a[x[4]]||"")} (CNAE ${esc(x[16])})</div>
  <dl><dt>Consumo médio (12 meses)</dt><dd>${fmt(x[2],0)} kWh/mês</dd><dt>Pg sem análise de inversão</dt><dd><b>${fmt(x[17],0)} kW</b></dd>
  <dt>Sistema FV p/ zerar consumo</dt><dd>${fmt(x[3])} kWp</dd>
  <dt>Classe · grupo</dt><dd>${esc(x[5])} · ${esc(x[10])}</dd>${x[15]!=null?`<dt>Demanda contratada</dt><dd>${fmt(x[15],0)} kW</dd>`:""}
  <dt>GD</dt><dd>${GDR[x[6]]}</dd><dt>Rede comporta o sistema</dt><dd>${RED[x[7]]}</dd>
  <dt>Horas sem energia no ano</dt><dd>${fmt(x[12])} h</dd><dt>Trafo · alimentador</dt><dd>${esc(x[13]||"–")} · ${esc(x[14]||"–")}</dd></dl>
  <div class="links"><a href="https://www.google.com/maps?q=${x[0]},${x[1]}" target="_blank" rel="noopener">Abrir no Google Maps</a>
  ${temTr?`<button class="ghost" data-tr="${esc(x[13])}">Ver trafo</button>`:""}</div>
  <div class="base">${x[11]?`Alimentador ${esc(x[14]||"–")} desenhado em azul no mapa${x._km?` (${fmt(x._km)} km de rede MT)`:""}.`
    :temTr?`Trafo ${esc(x[13])} destacado no mapa, com a rede BT em verde e a ligação até a empresa tracejada.`:"Trafo desta empresa não encontrado no mapa."}</div></div></section>`;
}
function baixarCsv(){
  const a=(empCache[META.mun]||{a:{}}).a,q=v=>`"${String(v??"").replace(/"/g,'""')}"`;
  const cab=["Endereço","Bairro","Município","Atividade","CNAE","Classe","Grupo","Rede","Consumo médio 12 meses (kWh/mês)","Pg sem análise de inversão (kW)","Sistema sugerido (kWp)","Demanda contratada (kW)","GD","Rede comporta","Horas sem energia/ano","Trafo","Alimentador","Latitude","Longitude","Google Maps"];
  const lin=empVis.map(x=>[x[8],x[9],META.munNome.split(",")[0],a[x[4]]||"",x[16],x[5],x[10],x[11]?"MT":"BT",x[2],x[17],String(x[3]).replace(".",","),x[15]??"",GDR[x[6]],RED[x[7]],String(x[12]??"").replace(".",","),x[13],x[14],x[0],x[1],`https://www.google.com/maps?q=${x[0]},${x[1]}`].map(q).join(";"));
  const blob=new Blob(["\ufeff"+[cab.map(q).join(";"),...lin].join("\r\n")],{type:"text/csv;charset=utf-8"});
  const l=document.createElement("a");l.href=URL.createObjectURL(blob);l.download=`prospeccao_${META.munNome.split(",")[0].replace(/\W+/g,"_")}.csv`;l.click();setTimeout(()=>URL.revokeObjectURL(l.href),2000);
}
async function geo(texto,bb){
  const vb=bb?`&viewbox=${bb[1]},${bb[2]},${bb[3]},${bb[0]}&bounded=1`:"";
  const r=await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&countrycodes=br${vb}&q=${encodeURIComponent(texto)}`,{headers:{"Accept-Language":"pt-BR"}});
  return r.json();
}

document.getElementById("f").addEventListener("submit",async e=>{
  e.preventDefault();const q=document.getElementById("q").value.trim();if(!q)return;
  const c=q.match(/^\s*(-?\d{1,2}[.,]\d+)\s*[,; ]\s*(-?\d{1,3}[.,]\d+)\s*$/);
  if(c){pontoGlobal(+c[1].replace(",","."),+c[2].replace(",","."));return}
  const up=q.toUpperCase();
  const t=D.find(x=>String(x.id).toUpperCase()===up)||D.find(x=>String(x.id).toUpperCase().includes(up)&&up.length>=3);
  if(t){abrir(t);return}
  const a=[...alimEl.options].find(o=>o.value&&o.value.toUpperCase()===up);
  if(a){alimEl.value=a.value;sel=null;ponto=null;desenhar();render();ajustar();return}
  msg="Buscando endereço…";render();
  try{
    let j=await geo(q+", "+META.munNome,META.bbox);
    if(!j.length&&META.muns)j=await geo(q+", RS",META.bboxAll);
    if(j.length){await pontoGlobal(+j[0].lat,+j[0].lon,j[0].display_name)}
    else{msg="Endereço não encontrado. Tente rua e número, ou cole a coordenada (ex.: -29.6842, -53.8069).";render()}
  }catch(err){msg="Não consegui buscar o endereço agora (sem internet?). Cole a coordenada ou clique no mapa.";render()}
});
const uc1El=document.getElementById("uc1"),filtraEl=document.getElementById("filtra"),filtraRow=document.getElementById("filtraRow");
// resumo dos filtros ativos no título do menu (útil com o menu recolhido)
function resumoFiltros(){
  const f=[],pot=+potEl.value||0;
  if(pot>0)f.push(`GD ${fmt(pot)} kW`);if(alimEl.value)f.push(alimEl.value);
  if(kvaMinEl.value||kvaMaxEl.value)f.push(`${kvaMinEl.value?fmtK(+kvaMinEl.value):"…"}–${kvaMaxEl.value?fmtK(+kvaMaxEl.value):"…"} kVA`);
  if(uc1El.checked)f.push("1 UC");if(document.getElementById("alimOk").checked)f.push("alim. comporta");
  document.getElementById("ftrN").textContent=f.length?"· "+f.join(" · "):"";
}
function aoMudarPot(){resumoFiltros();filtraRow.hidden=!((+potEl.value||0)>0);if(ponto){const p=proximos(ponto[0],ponto[1],1)[0];if(sel&&!visiveis().includes(sel))sel=p?p.t:null}desenhar();render()}
potEl.addEventListener("input",aoMudarPot);uc1El.addEventListener("change",aoMudarPot);
kvaMinEl.addEventListener("change",aoMudarPot);kvaMaxEl.addEventListener("change",aoMudarPot);
document.getElementById("alimOk").addEventListener("change",aoMudarPot);filtraEl.addEventListener("change",aoMudarPot);
alimEl.addEventListener("change",()=>{sel=null;resumoFiltros();desenhar();render();ajustar()});
const side=document.getElementById("side"),tog=document.getElementById("tog");
tog.onclick=()=>{side.classList.toggle("min");tog.setAttribute("aria-expanded",!side.classList.contains("min"));setTimeout(()=>map.invalidateSize(),50)};
function abrirPainel(){if(side.classList.contains("min")){side.classList.remove("min");setTimeout(()=>map.invalidateSize(),50)}}
function ajustar(){const v=visiveis();if(v.length)map.fitBounds(v.map(t=>[t.lat,t.lon]),{padding:[20,20]})}
matchMedia("(prefers-color-scheme:dark)").addEventListener?.("change",()=>desenhar());
const munEl=document.getElementById("mun");
if(META.muns){
  $("prosp").hidden=false;
  ["empOn","empAtv","empTen","empGd","empRede"].forEach(i=>$(i).addEventListener("change",()=>{empSel=null;desenharEmp()}));
  $("empKwh").addEventListener("input",()=>{empSel=null;desenharEmp()});
  $("empHideTr").addEventListener("change",()=>{if($("empHideTr").checked)map.removeLayer(capa);else capa.addTo(map)});
  $("empCsv").addEventListener("click",()=>{if(!$("empOn").checked){$("empOn").checked=true;desenharEmp().then(baixarCsv)}else baixarCsv()});
  document.getElementById("munRow").style.display="";
  META.muns.forEach(m=>{const o=document.createElement("option");o.value=m.c;o.textContent=m.n;munEl.append(o)});
  munEl.addEventListener("change",()=>carregarMun(munEl.value));
  const b=META.bboxAll;map.fitBounds([[b[0],b[1]],[b[2],b[3]]]);
  const h=location.hash.slice(1);carregarMun(META.muns.some(m=>m.c===h)?h:META.default);
}else{desenhar();render();ajustar()}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
