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
TTEN_KV = {"49": 13.8, "62": 22.0, "63": 23.0}
ENE = [f"ENE_{i:02d}" for i in range(1, 13)]
POT = [f"POT_{i:02d}" for i in range(1, 97)]
DIAS_MES = np.array([31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])
# Geração FV típica no RS (kWh/kWp por mês), escalada para ~1.150 kWh/kWp·ano
GER_FV = np.array([150, 130, 125, 100, 80, 65, 72, 92, 105, 128, 145, 155]) * 1150 / 1347
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
KVA_PADRAO = {3, 5, 10, 15, 25, 30, 37.5, 45, 50, 75, 100, 112.5, 150, 200, 225,
              250, 300, 400, 500, 750, 1000, 1500, 2000}
T0 = time.time()
PICO_MAX_CURVA = 8.0
# Curvas típicas por classe (scripts/curvas_padrao.json, tiradas da BDGD da RGE): usadas quando a UC
# não tem curva própria (várias cooperativas publicam a BDGD com CRVCRG vazia e TIP_CC em branco).
try:
    CURVAS_PADRAO = {f"CL_{k}": np.array(v) for k, v in json.loads(
        (Path(__file__).resolve().parent / "curvas_padrao.json").read_text(encoding="utf-8"))["curvas"].items()}
except (OSError, ValueError, KeyError):
    CURVAS_PADRAO = {}


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


def classe_curva(clas_sub):
    """Classe da UC (CLAS_SUB: RE1, CO3, RU2, IN, PP1, SP2, IP, CPR...) → chave da curva padrão."""
    c = (clas_sub or "").strip().upper()[:2]
    return {"CP": "CO"}.get(c, c) if c in ("RE", "CO", "CP", "IN", "RU", "PP", "SP", "IP") else ""


# Quais TIP_CC trocar pela curva da classe: None = todas (BDGD sem CRVCRG); senão só as curvas
# descartadas. Assim uma BDGD com curvas próprias boas (RGE, CEEE) não muda de resultado.
TROCAR_TIP = None


def tipologia(tip_cc, classe, curvas):
    """TIP_CC da UC, ou a curva padrão da classe dela quando a BDGD não tem a curva."""
    tip_cc = txt(tip_cc)
    if not isinstance(classe, pd.Series):
        classe = pd.Series(classe, index=tip_cc.index)
    chave = "CL_" + classe.astype(str)
    usar = ~tip_cc.isin(curvas.keys()) & chave.isin(curvas.keys())
    if TROCAR_TIP is not None:
        usar &= tip_cc.isin(TROCAR_TIP)
    return tip_cc.where(~usar, chave)


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
            d = ler(gdb, L[chave], ["CTMT", "SIT_ATIV", "TIP_CC", "CLAS_SUB"] + ENE, where=filtro)
        except Exception:
            continue
        if "SIT_ATIV" in d.columns:
            d = d[txt(d["SIT_ATIV"]).str.upper().isin(["AT", ""])]
        d["TRAFO"] = txt(d["CTMT"])
        d = d[d["TRAFO"].isin(set(ct["COD_ID"]))]
        d["TIP_CC"] = txt(d["TIP_CC"]) if "TIP_CC" in d.columns else ""
        cl = "IP" if chave == "pip" else (txt(d["CLAS_SUB"]).map(classe_curva) if "CLAS_SUB" in d.columns else "")
        d["TIP_CC"] = tipologia(d["TIP_CC"], cl, curvas)
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
            ct.loc[ct["I"] < 50, "I"] = np.nan     # cabo de saída < 50 A: erro de cadastro, fica sem dado
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
        "f": fases(r.get("Fases primário")),
    } for _, r in pts.iterrows()]


def fases(fas_con):
    """FAS_CON (ABC, ABCN, AB, AN...) → nº de fases (1, 2 ou 3); None sem dado."""
    n = sum(1 for x in "ABC" if x in str(fas_con or "").upper())
    return n or None


def _html(dados_js, meta, titulo):
    return (TEMPLATE.replace("__DATA__", dados_js).replace("__META__", _js(meta))
            .replace("__MUNNOME__", titulo).replace("__BASE__", meta["base"])
            .replace("__DIST__", meta.get("distTexto") or meta.get("titulo") or "distribuidora"))


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


INV_CAMPOS = ("id", "lat", "lon", "kva", "gd", "cmin", "capa", "alim", "sub", "f")


def resumo_alimentadores(pts):
    """dados/alim.json: [{a, s, lat, lon, b, capa, gda, ca, cga, n, i, i3}] — i = trafos com 1 UC
    (locais de usina) e i3 = desses, os trifásicos. A folga (capa) > 0 = alimentador sem inversão de fluxo."""
    col = "Alimentador (CTMT)"
    p = pts[pts[col].notna() & (pts[col].astype(str) != "")].copy()
    p["_f"] = p["Fases primário"].map(fases) if "Fases primário" in p.columns else None
    p["_u1"] = p["Nº UCs"] == 1
    saida = []
    for al, g in p.groupby(col):
        r = g.iloc[0]
        saida.append({
            "a": str(al), "s": "" if pd.isna(r.get("Subestação")) else str(r.get("Subestação")),
            "lat": round(float(g["lat"].median()), 5), "lon": round(float(g["lon"].median()), 5),
            "b": [round(float(g["lat"].min()), 4), round(float(g["lon"].min()), 4),
                  round(float(g["lat"].max()), 4), round(float(g["lon"].max()), 4)],
            "capa": _n(r.get("Capacidade disponível p/ GD no alimentador (kW)"), 0),
            "gda": _n(r.get("GD no alimentador (kW)"), 0),
            "ca": _n(r.get("Carregamento do alimentador (%)"), 1),
            "cga": _n(r.get("Carregamento de GD do alimentador (%)"), 1),
            "n": int(len(g)), "i": int(g["_u1"].sum()), "i3": int((g["_u1"] & (g["_f"] == 3)).sum()),
        })
    saida.sort(key=lambda x: -(x["capa"] or 0))
    return saida


def salvar_site(df, pasta, meta, padrao="4316907"):
    """Área inteira: index.html + dados/<código IBGE>.json (carregado sob demanda por município).
    Precisa ser servido por HTTP (GitHub Pages); aberto direto do disco o navegador bloqueia o fetch."""
    pasta = Path(pasta)
    (pasta / "dados").mkdir(parents=True, exist_ok=True)
    pts = df.dropna(subset=["lat", "lon"])
    muns, inv = [], []
    for (cod, nome), g in pts.groupby(["Município (IBGE)", "Município"]):
        regs = _registros(g)
        (pasta / "dados" / f"{cod}.json").write_text(_js(regs), encoding="utf-8")
        muns.append({"c": str(cod), "n": str(nome), "b": _bbox(g), "t": len(g),
                     "gd": round(float(g["GD instalada (kW)"].sum()), 1)})
        inv += [{k: r[k] for k in INV_CAMPOS} | {"m": str(cod)}
                for r in regs if r["ucs"] == 1 and (r["capa"] or 0) > 0]
    # "Usinas de investimento" (toda a área): trafos com 1 UC em alimentador com folga, maior folga primeiro
    inv.sort(key=lambda r: -r["capa"])
    (pasta / "dados" / "invest.json").write_text(_js(inv), encoding="utf-8")
    # Visão do estado: um resumo por alimentador (centro e retângulo dos trafos, folga, GD, locais de usina)
    (pasta / "dados" / "alim.json").write_text(_js(resumo_alimentadores(pts)), encoding="utf-8")
    chave = lambda m: unicodedata.normalize("NFKD", m["n"]).encode("ascii", "ignore").decode().lower()
    muns.sort(key=chave)
    meta.update(muns=muns, bboxAll=_bbox(pts), bbox=None, mun=None,
                default=padrao if any(m["c"] == padrao for m in muns) else muns[0]["c"])
    # lista de municípios desta distribuidora, usada pelo scripts/unificar.py (página com várias)
    (pasta / "dados" / "meta.json").write_text(_js(meta), encoding="utf-8")
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
    ap.add_argument("--padrao", default="4316907", help="com --mun todos: município que o site abre (código IBGE)")
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
                                          "CTMT", "TIP_CC", "CEG_GD", "CLAS_SUB"] + ENE, mun)
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
    uc["CL"] = txt(uc["CLAS_SUB"]).map(classe_curva) if "CLAS_SUB" in uc.columns else ""
    partes = [uc[["TRAFO", "TIP_CC", "CL"] + ENE]]
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
                pip["CL"] = "IP"
                partes.append(pip[["TRAFO", "TIP_CC", "CL"] + ENE])
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
    # curva com pico > 8× a média é medição isolada/erro (algumas permissionárias): usa a da classe
    ruins = [k for k, v in curvas.items() if v.max() > PICO_MAX_CURVA]
    for k in ruins:
        del curvas[k]
    if ruins:
        log(f"  {len(ruins)} curvas descartadas (pico > {PICO_MAX_CURVA:g}× a média)")
    global TROCAR_TIP
    TROCAR_TIP = set(ruins) if curvas else None
    sem_tip = ~cargas["TIP_CC"].isin(curvas.keys())
    curvas.update(CURVAS_PADRAO)
    cargas["TIP_CC"] = tipologia(cargas["TIP_CC"], cargas["CL"], curvas)
    if sem_tip.any() and CURVAS_PADRAO:
        log(f"  {(sem_tip & cargas['TIP_CC'].str.startswith('CL_')).mean() * 100:.0f}% das UCs sem curva própria: "
            "usada a curva padrão da classe")
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
        n_mun = salvar_site(df, site, meta, padrao=a.padrao)
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
.selo{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;box-shadow:0 1px 4px rgba(0,0,0,.2);max-width:70vw}
.selo .velha{color:var(--low);font-weight:600}
/* busca de cidade: digita e escolhe na lista filtrada */
.busca-mun{position:relative;flex:1;min-width:0}
#munBusca{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit;min-height:40px}
#munLista{position:absolute;z-index:1000;left:0;right:0;top:calc(100% + 2px);margin:0;padding:4px 0;list-style:none;background:var(--panel);
  border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.18);max-height:264px;overflow-y:auto}
#munLista[hidden]{display:none}
#munLista li{padding:9px 12px;cursor:pointer;font-size:14px;min-height:40px;box-sizing:border-box}
#munLista li[aria-selected="true"],#munLista li[data-k]:hover{background:var(--chip)}
#munLista li.vazio{color:var(--muted);cursor:default}
#munLista mark{background:none;color:var(--accent);font-weight:700}
/* aba Medir */
#medUso,#medW{width:76px;font-family:var(--mono)}#medC,#medL{width:76px;font-family:var(--mono)}
.acoes button[aria-pressed="false"]{background:var(--chip);color:var(--ink)}
.lado{background:rgba(20,20,20,.75)!important;color:#fff!important;border:0!important;box-shadow:none!important;font:600 11px var(--mono)!important;padding:1px 4px!important}
.lado::before{display:none}
.vert{background:#fff;border:2px solid #f2b400;border-radius:50%;box-shadow:0 0 0 1px rgba(0,0,0,.5)}
.medGrande{font:600 30px var(--mono);font-variant-numeric:tabular-nums}.medGrande small{font-size:15px;color:var(--muted)}
.glogo img{display:block;height:18px;filter:drop-shadow(0 0 1px rgba(0,0,0,.6))}
.links a.gsat{display:inline-block;background:var(--accent);color:var(--accent-ink);padding:4px 10px;border-radius:6px;text-decoration:none;font-weight:600}
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
/* Abas: Consultar · Prospectar · Sobre os dados */
header{padding-bottom:0}
.tabs{display:flex;gap:2px;margin:0 -6px;flex-wrap:wrap}
.tabs button{background:none;color:var(--muted);border-radius:0;border-bottom:3px solid transparent;padding:10px 4px 9px;min-height:44px;
  font:700 14px var(--display);letter-spacing:.04em;text-transform:uppercase}
.tabs button[aria-selected="true"]{color:var(--accent);border-bottom-color:var(--accent)}
.tabs button[hidden]{display:none}
.aba{display:flex;flex-direction:column;min-width:0}
.aba[hidden]{display:none}
.aba>.consulta,.aba>details.menu,.aba>.painel{margin:14px 18px 0}
.consulta{background:var(--chip);border-radius:12px;padding:14px;display:grid;gap:10px}
.painel{display:grid;gap:10px}
.pergunta{margin:0;font:700 19px/1.2 var(--display);text-transform:uppercase;letter-spacing:.03em}
.campo{display:grid;gap:4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;min-width:0}
.campo input{padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:15px var(--body);text-transform:none;letter-spacing:0;min-width:0}
.linha{display:flex;gap:10px;align-items:flex-end}.linha .campo{flex:1}
.linha button{min-height:44px;padding:0 18px;font-size:15px}
#pot{width:100%;font:600 16px var(--mono)}
.sobre p{margin:0;font-size:14px}.sobre h2{margin:6px 0 0}.sobre a{color:var(--accent)}
.sobre dl{grid-template-columns:auto 1fr}.sobre dd{text-align:left;font-family:var(--body)}
/* Resposta da consulta (sinal: cor + forma) */
.ico{flex:none;vertical-align:middle}
.ver{display:flex;gap:12px;align-items:center;border:2px solid var(--c);border-radius:12px;padding:12px 14px;
  background:color-mix(in srgb,var(--c) 12%,var(--panel))}
.ver strong{display:block;font:700 22px/1.1 var(--display);text-transform:uppercase}
.ver span{font-size:14px}
.duo{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}
.pill{display:inline-flex;align-items:center;gap:5px}.pill .ico{stroke:#fff!important}
.near button{grid-template-columns:16px 1fr auto}
.acoes{display:flex;gap:8px;flex-wrap:wrap}.acoes button{min-height:40px}
details.mais summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:13px}
details.mais[open] summary{margin-bottom:6px}
/* Ficha do trafo flutuando sobre o mapa (computador) */
.fichaPop .leaflet-popup-content-wrapper{background:var(--panel);color:var(--ink);border-radius:12px;box-shadow:0 8px 28px rgba(0,0,0,.25)}
.fichaPop .leaflet-popup-tip{background:var(--panel)}
.fichaPop .leaflet-popup-content{margin:0;width:330px!important;max-height:calc(100vh - 150px);overflow-y:auto;font:15px/1.45 var(--body)}
.fichaPop .card{border:0;padding:14px 16px}
.fichaPop a.leaflet-popup-close-button{width:32px;height:32px;font:22px/30px var(--body);color:var(--muted)}
.fichaPop .card .top{padding-right:22px;flex-wrap:wrap;justify-content:flex-start}
.fichaPop .card .id{word-break:normal;overflow-wrap:anywhere}
/* Controles sobre o mapa: data dos dados, legenda, dica de primeiro acesso */
.mapbox{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px;box-shadow:0 1px 4px rgba(0,0,0,.2)}
.legenda{display:grid;gap:3px}.legenda b{font:700 13px var(--display);text-transform:uppercase;letter-spacing:.06em}
.legenda div{display:flex;gap:7px;align-items:center}.legenda .base{font-size:11px}
.legenda.soCel{display:none;font-size:13px}
.dica{max-width:270px;background:var(--ink);color:var(--panel);border-radius:12px;padding:12px 14px;display:grid;gap:8px;box-shadow:0 8px 24px rgba(0,0,0,.25);font-size:14px}
.dica .base{color:inherit;opacity:.8}.dica .acoes{justify-content:flex-end}
.dica button.ghost{background:transparent;color:inherit;border:1px solid currentColor}
/* Usinas de investimento: botão na consulta, aba própria e círculos azuis no mapa */
.aba>.btInv{margin:10px 18px 0;min-height:44px;font-size:15px;display:flex;gap:8px;align-items:center;justify-content:center;border:1px solid var(--accent);color:var(--accent)}
.btInv[hidden]{display:none}
#invOnde{flex:1 1 0;width:0;min-width:110px}
#invKvaMin,#invKvaMax{flex:1 1 0;width:0;min-width:90px;font-family:var(--mono)}
.dot.inv{background:#1d4ed8;border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.35)}
@media (max-width:760px){
  body{grid-template-columns:1fr;grid-template-rows:1fr auto}
  #map{grid-row:1}
  body{grid-template-columns:minmax(0,1fr)}
  aside{grid-row:2;height:auto;max-height:58vh;border-right:0;border-top:1px solid var(--line);
        padding-bottom:env(safe-area-inset-bottom,0px);overflow-y:auto}
  aside .scroll{overflow:visible;flex:none}
  aside.min{overflow:hidden}aside.min header>*:not(.brand){display:none}
  aside.min .scroll,aside.min .aba{display:none}
  .aba>.consulta,.aba>details.menu,.aba>.painel{margin:12px 16px 0}
  .aba>.btInv{margin:10px 16px 0}
  .leaflet-control.legenda{display:none}.legenda.soCel{display:grid}
  .dica{max-width:220px;font-size:13px}
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
    <div class="base">Dados da BDGD de __BASE__</div>
    <div class="row" id="distRow" style="display:none">
      <label for="dist">Distribuidora</label>
      <select id="dist"></select>
    </div>
    <div class="row" id="munRow" style="display:none">
      <label for="munBusca">Município</label>
      <div class="busca-mun">
        <input id="munBusca" type="search" autocomplete="off" spellcheck="false" placeholder="Digite o nome da cidade"
          role="combobox" aria-expanded="false" aria-controls="munLista" aria-autocomplete="list">
        <ul id="munLista" role="listbox" aria-label="Cidades" hidden></ul>
      </div>
      <select id="mun" hidden aria-hidden="true" tabindex="-1"></select>
    </div>
    <nav class="tabs" role="tablist" aria-label="Seções">
      <button type="button" role="tab" id="tabC" data-tab="C" aria-selected="true" aria-controls="abaC">Consultar</button>
      <button type="button" role="tab" id="tabP" data-tab="P" aria-selected="false" aria-controls="abaP" hidden>Prospectar</button>
      <button type="button" role="tab" id="tabI" data-tab="I" aria-selected="false" aria-controls="abaI" hidden>Usinas</button>
      <button type="button" role="tab" id="tabM" data-tab="M" aria-selected="false" aria-controls="abaM">Medir</button>
      <button type="button" role="tab" id="tabS" data-tab="S" aria-selected="false" aria-controls="abaS">Sobre os dados</button>
    </nav>
  </header>
  <section class="aba" id="abaC" role="tabpanel" aria-labelledby="tabC">
    <form class="consulta" id="f">
      <p class="pergunta">Onde você quer ligar GD?</p>
      <label class="campo" for="q">Endereço, coordenada, trafo ou alimentador
        <input id="q" type="search" placeholder="Ex.: Rua do Acampamento, 100" autocomplete="off"></label>
      <div class="linha">
        <label class="campo" for="pot">Potência pretendida (kW)
          <input id="pot" type="number" min="0" step="0.1" placeholder="kW"></label>
        <button type="submit">Consultar</button>
      </div>
      <p class="hint">Ou clique no mapa no local da instalação.</p>
      <label class="chk" id="filtraRow" hidden><input type="checkbox" id="filtra" checked> Mostrar no mapa só os trafos que comportam essa potência</label>
    </form>
    <button type="button" class="ghost btInv" id="btInv" hidden><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg> Usinas de investimento</button>
    <details id="ftr" class="menu">
    <summary>Filtros avançados <span class="base" id="ftrN"></span></summary>
    <div class="row">
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
    </details>
    <div class="scroll" id="info"></div>
  </section>
  <section class="aba" id="abaP" role="tabpanel" aria-labelledby="tabP" hidden>
    <div id="prosp" class="painel">
      <p class="pergunta">Empresas com perfil para GD</p>
      <label class="chk"><input type="checkbox" id="empOn"> Mostrar empresas no mapa</label>
      <label class="chk"><input type="checkbox" id="empHideTr"> Esconder os trafos do mapa</label>
      <div class="row"><label for="empAtv">Atividade</label><select id="empAtv"><option value="">Todas</option></select></div>
      <div class="row"><label for="empKwh">Consumo mín.</label><input id="empKwh" type="number" min="0" step="100" value="1000"><span class="base">kWh/mês</span>
        <label for="empTen">Rede</label><select id="empTen"><option value="">BT e MT</option><option value="0">BT</option><option value="1">MT</option></select></div>
      <div class="row"><label for="empGd">GD</label><select id="empGd"><option value="0">Sem GD</option><option value="1">Sem GD ou talvez</option><option value="">Todas</option></select>
        <label for="empRede">Rede comporta</label><select id="empRede"><option value="">Qualquer</option><option value="3">Sim</option><option value="2">Sim ou parcial</option></select></div>
      <button type="button" class="ghost" id="empCsv">Baixar lista (CSV)</button>
    </div>
    <div class="scroll" id="infoEmp"></div>
  </section>
  <section class="aba" id="abaM" role="tabpanel" aria-labelledby="tabM" hidden>
    <div class="painel">
      <p class="pergunta">Medir telhado</p>
      <p class="hint" id="medDica">Toque num telhado no mapa: o contorno aparece sozinho. Se não aparecer, ou para medir só uma parte, use "Desenhar contorno".</p>
      <div class="acoes">
        <button type="button" id="medToque" aria-pressed="true">Tocar no telhado</button>
        <button type="button" class="ghost" id="medDesenho" aria-pressed="false">Desenhar contorno</button>
        <button type="button" class="ghost" id="medLimpa">Limpar</button>
      </div>
      <div class="row"><label for="medInc">Inclinação</label>
        <select id="medInc">
          <option value="0">0° · laje ou plano</option>
          <option value="6">6° · fibrocimento / metálica (10%)</option>
          <option value="17" selected>17° · telha cerâmica (30%)</option>
          <option value="22">22° · telha cerâmica (40%)</option>
          <option value="27">27° · telhado íngreme (50%)</option>
        </select></div>
      <div class="row"><label for="medUso">Aproveitamento</label><input id="medUso" type="number" min="10" max="100" step="5" value="60"><span class="base">% da área</span></div>
      <div class="row"><label for="medW">Módulo</label><input id="medW" type="number" min="100" max="900" step="5" value="585"><span class="base">W</span>
        <input id="medC" type="number" min="0.5" max="3" step="0.01" value="2.28" aria-label="comprimento do módulo (m)"><span class="base">×</span>
        <input id="medL" type="number" min="0.5" max="2" step="0.01" value="1.13" aria-label="largura do módulo (m)"><span class="base">m</span></div>
    </div>
    <div class="scroll" id="infoMed"></div>
  </section>
  <section class="aba" id="abaI" role="tabpanel" aria-labelledby="tabI" hidden>
    <div class="painel">
      <p class="pergunta">Usinas de investimento</p>
      <p class="hint">Trafos com um único consumidor, ligados a alimentador com folga para nova GD, do maior para o menor.</p>
      <div class="row"><label for="invOnde">Onde</label><select id="invOnde"><option value="m">Município selecionado</option><option value="t">Toda a área da __DIST__</option></select></div>
      <div class="row"><label for="invKvaMin">Trafo de</label><select id="invKvaMin"><option value="">mín.</option></select>
        <label for="invKvaMax">até</label><select id="invKvaMax"><option value="">máx.</option></select><span class="base">kVA</span></div>
      <label class="chk"><input type="checkbox" id="invTri"> Só trafo trifásico</label>
      <label class="chk"><input type="checkbox" id="invTr"> Exigir folga também no trafo</label>
      <label class="chk"><input type="checkbox" id="invMapa" checked> Mostrar os locais no mapa</label>
      <div class="row"><label for="invAlim">Alimentadores</label><select id="invAlim"><option value="ok">Só os sem inversão de fluxo</option><option value="todos">Todos (verde e vermelho)</option><option value="">Não mostrar</option></select></div>
      <button type="button" class="ghost" id="invCsv">Baixar lista (CSV)</button>
    </div>
    <div class="scroll" id="infoInv"></div>
  </section>
  <section class="aba" id="abaS" role="tabpanel" aria-labelledby="tabS" hidden>
    <div class="scroll sobre">
      <p class="pergunta">De onde vêm os números</p>
      <dl><dt>Rede e cargas</dt><dd>BDGD da __DIST__ (ANEEL), data-base __BASE__</dd>
      <dt>GD já ligada</dt><dd>Base de GD da ANEEL</dd></dl>
      <h2>Capacidade do trafo</h2>
      <p>Menor valor entre a carga mínima de dia (o consumo dos vizinhos ao meio-dia, × fator de segurança) e a potência do trafo, descontada a GD que já existe. Acima da carga mínima, a energia volta para a rede (inversão de fluxo).</p>
      <h2>Capacidade do alimentador</h2>
      <p>Menor valor entre a carga mínima de dia do alimentador e a capacidade do cabo de saída, descontada a GD em BT e MT. O carregamento do alimentador é a demanda de pico ÷ capacidade do cabo de saída; valores muito acima de 100% indicam dado atípico na BDGD.</p>
      <h2>Prospecção</h2>
      <p>Pg = consumo médio de 12 meses ÷ (16% × 24 × 30) × fator da classe, arredondado para cima (REN 1.000, art. 73-A, II; Manual de Inversão de Fluxo, item 8.12). Vale para microgeração até 75 kW. Na abordagem comercial, respeite a LGPD e o cadastro “Não Perturbe”.</p>
      <h2>Medição de telhados</h2>
      <p>Contornos dos prédios do Overture Maps, que junta OpenStreetMap (desenhado por voluntários), Google Open Buildings e Microsoft (detectados por inteligência artificial em imagens de satélite). A área é a projeção vista de cima; a inclinação escolhida converte para a área real do telhado. O contorno pode estar deslocado ou desatualizado em relação à imagem: confira e, se preciso, arraste os cantos ou desenhe à mão. Licença ODbL: © OpenStreetMap contributors, Overture Maps Foundation.</p>
      <h2>Usinas de investimento</h2>
      <p>Trafos com uma única unidade consumidora cujo alimentador ainda tem folga para nova GD, ordenados pela maior folga do alimentador. A folga é do alimentador inteiro: locais do mesmo alimentador disputam a mesma folga. Como o trafo de um só consumidor costuma ser pequeno, a usina em geral exige trocar ou ampliar o trafo; marque “exigir folga também no trafo” para ver só os que já comportam.</p>
      <h2>Limitações</h2>
      <p>Não considera proteção nem manobras da rede. Consumo de quem já tem GD é o líquido medido. É uma triagem: quem decide a conexão é a distribuidora.</p>
      <p><a href="https://github.com/tfmachado19/capacidade-hospedagem/issues" target="_blank" rel="noopener">Relatar erro nos dados ou pedir melhoria</a></p>
    </div>
  </section>
</aside>
<div id="map"></div>
<script>
// Mesmo arquivo serve o site oficial (raiz) e o beta (/beta/): os dados ficam sempre na raiz.
const BETA=/\/beta\/?$/.test(location.pathname.replace(/index\.html$/,"")),BASE=BETA?"../":"";
if(BETA){document.title="[BETA] "+document.title;const h=document.querySelector("h1");
  if(h)h.insertAdjacentHTML("afterbegin",'<span style="background:#c62828;color:#fff;border-radius:4px;padding:1px 6px;margin-right:6px;font-size:13px;vertical-align:middle">BETA</span>');}
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
// Cada classe tem também uma forma (círculo, losango, triângulo, quadrado), para quem não distingue as cores
const FORMA={ok:"c",mid:"d",low:"t",none:"q",nd:"c"};
function icone(k,s=14){
  const f=FORMA[k],sh=f==="q"?`<rect x="2" y="2" width="12" height="12" rx="2"/>`:f==="t"?`<path d="M8 1.5L15 14.5H1z"/>`:
    f==="d"?`<path d="M8 .8L15.2 8 8 15.2.8 8z"/>`:`<circle cx="8" cy="8" r="6.5"/>`;
  return `<svg class="ico" width="${s}" height="${s}" viewBox="0 0 16 16" aria-hidden="true" style="fill:${COR[k]};stroke:rgba(0,0,0,.35);stroke-width:1">${sh}</svg>`;
}
L.Canvas.include({_updateForma(l){
  if(!this._drawing||l._empty())return;
  const p=l._point,c=this._ctx,r=Math.max(Math.round(l._radius),1),f=l.options.forma;
  c.beginPath();
  if(f==="q")c.rect(p.x-r*.85,p.y-r*.85,r*1.7,r*1.7);
  else if(f==="t"){c.moveTo(p.x,p.y-r);c.lineTo(p.x+r,p.y+r*.8);c.lineTo(p.x-r,p.y+r*.8);c.closePath()}
  else{c.moveTo(p.x,p.y-r);c.lineTo(p.x+r,p.y);c.lineTo(p.x,p.y+r);c.lineTo(p.x-r,p.y);c.closePath()}
  this._fillStroke(c,l);
}});
const Forma=L.CircleMarker.extend({_updatePath(){
  if(this.options.forma!=="c"&&this._renderer._updateForma)this._renderer._updateForma(this);else this._renderer._updateCircle(this)}});

const map=L.map("map",{preferCanvas:true,zoomControl:false});
const osm=L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:20,maxNativeZoom:19,attribution:"© OpenStreetMap · BDGD/ANEEL"});
const sat=L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{maxZoom:20,maxNativeZoom:19,attribution:"Imagens © Esri · BDGD/ANEEL"});
osm.addTo(map);const camadas=L.control.layers({"Mapa":osm,"Satélite":sat},null,{position:"topright"}).addTo(map);
L.control.zoom({position:"topright"}).addTo(map);
// Sobre o mapa: data dos dados (sempre visível), legenda e dica de primeiro acesso
function caixa(pos,cls,html){const c=L.control({position:pos});
  c.onAdd=()=>{const d=L.DomUtil.create("div",cls);d.innerHTML=html;L.DomEvent.disableClickPropagation(d);L.DomEvent.disableScrollPropagation(d);return d};
  return c.addTo(map)}
caixa("topleft","mapbox",`<b>Dados de ${esc(META.base)}</b> · estimativa para triagem`);
const legCtl=caixa("bottomleft","mapbox legenda","");
const DICAS=["Digite o endereço (ou clique no mapa) e a potência que quer ligar. O painel diz se cabe.",
  "Clique num trafo para abrir a ficha, com a capacidade e os botões para compartilhar e baixar em PDF.",
  "Na aba Prospectar estão as empresas com perfil para GD. Em Sobre os dados, como os números são calculados."];
let dicaN=0;try{if(localStorage.getItem("dicaVista"))dicaN=-1}catch(e){}
const dicaCtl=dicaN<0?null:caixa("topleft","dica","");
function mostrarDica(){
  if(!dicaCtl)return;const d=dicaCtl.getContainer();
  if(dicaN<0||dicaN>=DICAS.length){dicaCtl.remove();try{localStorage.setItem("dicaVista","1")}catch(e){}return}
  d.innerHTML=`<span class="base">Primeiro acesso · ${dicaN+1} de ${DICAS.length}</span><span>${DICAS[dicaN]}</span>
  <div class="acoes"><button type="button" class="ghost" data-d="-1">Pular</button><button type="button" data-d="${dicaN+1}">${dicaN<DICAS.length-1?"Próximo":"Entendi"}</button></div>`;
  d.querySelectorAll("button").forEach(b=>b.onclick=()=>{dicaN=+b.dataset.d;mostrarDica()});
}
mostrarDica();
// ---- Satélite Google (Map Tiles API oficial). A chave fica em dados/config.json:
// {"googleMapsKey": "..."} — chave restrita ao domínio do site e à Map Tiles API.
let gTiles=null,gKey="",gSessao="";
const gLogo=L.control({position:"bottomright"});
gLogo.onAdd=()=>{const d=L.DomUtil.create("div","glogo");
  d.innerHTML='<img alt="Google" src="https://maps.gstatic.com/mapfiles/api-3/images/google_white5.png">';return d};
async function iniciarGoogle(){
  try{const c=await (await fetch(BETA?"config.json":"dados/config.json",{cache:"no-store"})).json();gKey=(c.googleMapsKey||"").trim()}catch(e){return}
  if(!gKey)return;
  try{
    const ant=JSON.parse(localStorage.getItem("gSessao")||"null");
    if(ant&&ant.k===gKey&&ant.exp*1000>Date.now()+864e5)gSessao=ant.s;
  }catch(e){}
  if(!gSessao){
    try{const r=await fetch(`https://tile.googleapis.com/v1/createSession?key=${gKey}`,{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({mapType:"satellite",language:"pt-BR",region:"BR"})});
      const j=await r.json();if(!j.session)throw new Error(JSON.stringify(j.error||j));
      gSessao=j.session;try{localStorage.setItem("gSessao",JSON.stringify({s:j.session,exp:+j.expiry,k:gKey}))}catch(e){}
    }catch(e){console.warn("Google Map Tiles:",e);return}
  }
  gTiles=L.tileLayer(`https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=${gSessao}&key=${gKey}`,
    {maxZoom:21,maxNativeZoom:21,attribution:"Imagens © Google"});
  camadas.addBaseLayer(gTiles,"Satélite Google");
}
iniciarGoogle();
let tAtrib=null;
async function atribGoogle(){
  if(!gTiles||!map.hasLayer(gTiles)){if(gLogo._map)gLogo.remove();return}
  if(!gLogo._map)gLogo.addTo(map);
  const b=map.getBounds();
  try{const j=await (await fetch(`https://tile.googleapis.com/tile/v1/viewport?session=${gSessao}&key=${gKey}&zoom=${map.getZoom()}`+
      `&north=${b.getNorth()}&south=${b.getSouth()}&east=${b.getEast()}&west=${b.getWest()}`)).json();
    const t="Imagens © Google"+(j.copyright?" · "+esc(j.copyright):"");
    if(gTiles.options.attribution!==t){map.attributionControl.removeAttribution(gTiles.options.attribution);gTiles.options.attribution=t;map.attributionControl.addAttribution(t)}
  }catch(e){}
}
map.on("moveend baselayerchange",()=>{clearTimeout(tAtrib);tAtrib=setTimeout(atribGoogle,400)});
// Data da imagem de satélite no centro do mapa (metadados públicos da Esri / World Imagery)
let metaUrl="https://metadata.maptiles.arcgis.com/arcgis/rest/services/World_Imagery_Metadata_2026_r09/MapServer";
fetch("https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json").then(r=>r.json()).then(d=>{
  const v=Object.values(d).filter(x=>x.metadataLayerUrl&&x.itemTitle).sort((a,b)=>a.itemTitle<b.itemTitle?1:-1)[0];
  if(v)metaUrl=v.metadataLayerUrl;}).catch(()=>{});
const selo=L.control({position:"bottomleft"});
selo.onAdd=()=>{const d=L.DomUtil.create("div","selo");d.style.display="none";L.DomEvent.disableClickPropagation(d);return d};
selo.addTo(map);let tSelo=null,nSelo=0;
async function dataImagem(){
  const el=selo.getContainer();
  if(gTiles&&map.hasLayer(gTiles)){el.style.display="";el.textContent="Satélite Google · o Google não informa a data da imagem";return}
  if(!map.hasLayer(sat)){el.style.display="none";return}
  el.style.display="";
  if(map.getZoom()<12){el.textContent="Aproxime o mapa para ver a data da imagem de satélite";return}
  const c=map.getCenter(),b=map.getBounds(),s=map.getSize(),n=++nSelo;
  const u=`${metaUrl}/identify?geometry=${c.lng},${c.lat}&geometryType=esriGeometryPoint&sr=4326&layers=all&tolerance=1`+
    `&mapExtent=${b.getWest()},${b.getSouth()},${b.getEast()},${b.getNorth()}&imageDisplay=${s.x},${s.y},96&returnGeometry=false&f=json`;
  try{
    const j=await (await fetch(u)).json();if(n!==nSelo)return;
    const a=((j.results||[])[0]||{}).attributes;
    if(!a){el.textContent="Data da imagem indisponível neste ponto";return}
    const d=String(a.SRC_DATE||""),dt=d.length===8?`${d.slice(6)}/${d.slice(4,6)}/${d.slice(0,4)}`:(a.SRC_DATE2||"?");
    const anos=d.length===8?(Date.now()-new Date(+d.slice(0,4),+d.slice(4,6)-1,+d.slice(6)))/3.156e10:0;
    const res=a.SRC_RES?`${Math.round(+a.SRC_RES*100)} cm`:"";
    el.innerHTML=`Imagem de satélite: <b>${dt}</b>${anos>=2?` <span class="velha">(${Math.floor(anos)} anos)</span>`:""}${res?` · ${res}`:""}${a.NICE_DESC?` · ${esc(a.NICE_DESC)}`:""}`;
  }catch(e){if(n===nSelo)el.textContent="Data da imagem indisponível agora"}
}
map.on("moveend baselayerchange",()=>{clearTimeout(tSelo);tSelo=setTimeout(dataImagem,350)});
const gSat=(la,lo)=>`https://www.google.com/maps/@?api=1&map_action=map&center=${la},${lo}&zoom=20&basemap=satellite`;

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
  try{if(!(f in mtCache)){const r=await fetch(`${DB}dados/mt/${f}.json`);mtCache[f]=r.ok?await r.json():null}}catch(e){mtCache[f]=null}
  if(empSel!==x)return;
  const d=mtCache[f];x._km=d?d.km:0;
  if(d)linhas(d,{color:"#1f5fbf",weight:4,opacity:.9,interactive:false,renderer:rTopo},redeMt);
  render();
}
function selEmp(x){
  empSel=x;fichaFechada=true;aba("P");redeMt.clearLayers();rede.clearLayers();ponto=null;if(pino){map.removeLayer(pino);pino=null}
  if(x[11]){sel=null;desenhar();render();mostrarAlim(x)}
  else{
    const t=D.find(t=>t.id===x[13]);sel=t||null;desenhar();render();
    if(t){mostrarRede(t);
      L.circleMarker([t.lat,t.lon],{radius:11,color:cssVar("--ink"),weight:3,fillColor:hex(classe(t,+potEl.value||0)),fillOpacity:1})
        .bindTooltip(()=>dicaTrafo(t),{direction:"top",offset:[0,-8]}).addTo(redeMt);
      L.polyline([[x[0],x[1]],[t.lat,t.lon]],{color:"#8e44ec",weight:2.5,dashArray:"5 5",interactive:false,renderer:rTopo}).addTo(redeMt)}
  }
  abrirPainel();topoPainel();
}
async function mostrarRede(t){
  rede.clearLayers();
  if(!t||!META.muns||!t.alim)return;
  const f=String(t.alim).replace(/[^\w.-]/g,"_");
  try{if(!(f in btCache)){const r=await fetch(`${DB}dados/bt/${f}.json`);btCache[f]=r.ok?await r.json():{}}}catch(e){return}
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
const potEl=document.getElementById("pot"),alimEl=document.getElementById("alim"),info=document.getElementById("info"),infoEmp=document.getElementById("infoEmp"),infoInv=document.getElementById("infoInv");
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
  const v=visiveis();if(sel&&!v.includes(sel))v.push(sel);
  v.forEach(t=>{
    const k=classe(t,pot),c=hex(k);
    const m=new Forma([t.lat,t.lon],{forma:FORMA[k],radius:t===sel?10:6,color:t===sel?cssVar("--ink"):c,weight:t===sel?3:1,fillColor:c,fillOpacity:.85});
    m.bindTooltip(()=>dicaTrafo(t),{direction:"top",offset:[0,-6]});
    m.on("click",e=>{L.DomEvent.stopPropagation(e);abrir(t)});capa.addLayer(m);marcas.set(t.id,m);
  });
  // por cima dos trafos: locais para usina de investimento (aba Usinas)
  invVis=INV?invFiltro():[];
  if(INV&&$("invMapa").checked&&(map.getZoom()>=ZOOM_LOCAIS||!$("invAlim").value))invVis.forEach(t=>{if(sel&&sel.id===t.id)return;
    const m=L.circleMarker([t.lat,t.lon],{radius:8,color:"#ffffff",weight:2.5,fillColor:"#1d4ed8",fillOpacity:1});
    m.bindTooltip(()=>dicaInv(t),{direction:"top",offset:[0,-6]});m.on("click",e=>{L.DomEvent.stopPropagation(e);abrirInv(t)});capa.addLayer(m)});
  if(ALIM)desenharAlim();
}
function resumo(){
  const v=base(),pot=+potEl.value||0;
  const oc=filtrando()?v.length-visiveis().length:0;
  const ca=alimEl.value&&v.length?v[0].ca:null;
  const gd=v.reduce((s,t)=>s+t.gd,0),kva=v.reduce((s,t)=>s+(t.kva||0),0);
  return `<section><h2>${alimEl.value?"Alimentador "+esc(alimEl.value):(META.muns?esc(META.munNome.split(",")[0]):"Município")}</h2>
  <div class="stats"><div class="stat"><b>${v.length.toLocaleString("pt-BR")}</b><span>trafos</span></div>
  <div class="stat"><b>${fmt(gd/1000,2)}</b><span>MW de GD</span></div>
  <div class="stat"><b>${fmt(kva/1000,2)}</b><span>MVA instalado</span></div></div>
  ${alimEl.value&&v.length?`<dl><dt>Carregamento do alimentador</dt><dd>${fmt(ca)} %</dd><dt>GD no alimentador</dt><dd>${fmt(v[0].gda,0)} kW</dd>
  <dt>Carregamento de GD do alimentador</dt><dd>${fmt(v[0].cga)} %</dd><dt>Capacidade p/ nova GD no alimentador</dt><dd>${fmt(v[0].capa,0)} kW</dd></dl>`:""}
  ${uc1El.checked?`<p class="hint">Mostrando só trafos com 1 unidade consumidora.</p>`:""}
  ${document.getElementById("alimOk").checked?`<p class="hint">Mostrando só trafos cujo alimentador comporta ${(+potEl.value||0)>0?fmt(+potEl.value)+" kW":"nova GD"}.</p>`:""}
  ${kvaMinEl.value||kvaMaxEl.value?`<p class="hint">Trafos de ${kvaMinEl.value?fmtK(+kvaMinEl.value):"qualquer"} a ${kvaMaxEl.value?fmtK(+kvaMaxEl.value):"qualquer"} kVA.</p>`:""}
  ${oc?`<p class="hint">${oc.toLocaleString("pt-BR")} trafos com capacidade abaixo de ${fmt(pot)} kW estão ocultos no mapa.</p>`:""}</section>`;
}
// legenda fixa no canto do mapa, com a contagem de trafos de cada classe
function legenda(){
  const v=base(),pot=+potEl.value||0,n={ok:0,mid:0,low:0,none:0,nd:0},r=pot>0?ROTP:ROT;v.forEach(t=>n[classe(t,pot)]++);
  return `<b>${pot>0?"Para "+fmt(pot)+" kW":"Capacidade p/ nova GD"}</b>`+
    Object.keys(r).map(k=>`<div>${icone(k)}${r[k]} <span class="base">(${n[k].toLocaleString("pt-BR")})</span></div>`).join("");
}
// resposta direta: a potência pretendida cabe no trafo mais próximo do ponto?
function maisProximo(lat,lon){let m=null;base().forEach(t=>{const d=dist(lat,lon,t.lat,t.lon);if(!m||d<m.d)m={t,d}});return m}
const fmtD=d=>d<1000?Math.round(d)+" m":fmt(d/1000,2)+" km";
function veredito(){
  const p=maisProximo(ponto[0],ponto[1]);if(!p)return "";
  const t=p.t,pot=+potEl.value||0,dd=`<div class="duo"><div class="stat"><b>${fmt(t.cap)} kW</b><span>trafo comporta</span></div>
    <div class="stat"><b>${fmt(t.capa,0)} kW</b><span>alimentador comporta</span></div></div>`;
  if(!(pot>0))return `<section>${dd}<p class="hint">Informe a potência pretendida para saber se cabe no trafo ${esc(t.id)} (a ${fmtD(p.d)}).</p></section>`;
  const k=classe(t,pot),aOk=t.capa==null?null:t.capa>=pot;let c,ic,tit,txt;
  if(k==="nd"){c="nd";tit="Sem dado de carga";txt=`O trafo mais próximo (${esc(t.id)}, a ${fmtD(p.d)}) não tem dado de carga na BDGD.`}
  else if(k==="ok"&&aOk!==false){c="ok";tit="Provavelmente cabe";txt=`${fmt(pot)} kW no trafo <b>${esc(t.id)}</b>, a ${fmtD(p.d)} do ponto.`}
  else if(k==="ok"){c="mid";tit="Cabe no trafo, alimentador no limite";txt=`O trafo ${esc(t.id)} comporta, mas o alimentador ${esc(t.alim||"")} comporta só ${fmt(t.capa,0)} kW de nova GD.`}
  else{c=k;tit="Não cabe no trafo mais próximo";txt=`O trafo ${esc(t.id)} (a ${fmtD(p.d)}) comporta ${fmt(t.cap)} kW. Veja abaixo os trafos vizinhos.`}
  return `<section aria-live="polite"><div class="ver" style="--c:${COR[c]}">${icone(c==="mid"?"mid":k,34)}<div><strong>${tit}</strong><span>${txt}</span></div></div>${dd}</section>`;
}
function rodape(){return `<p class="note">Estimativa para triagem a partir de dados públicos da ANEEL (BDGD de ${esc(META.base)}). Não substitui a análise da distribuidora. Veja como é calculado na aba “Sobre os dados”.</p>`}
const LIM={"inversão de fluxo":"Limitada pela carga mínima de dia (risco de inversão de fluxo)","potência do trafo":"Limitada pela potência do trafo","sem dado":"Sem dado de carga na BDGD"};
let detAberto=false;
function ficha(t){
  const pot=+potEl.value||0,k=classe(t,pot),r=pot>0?ROTP:ROT;
  return `<div class="card"><div class="top"><span class="id">Trafo ${esc(t.id)}</span><span class="pill" style="background:${COR[k]}">${icone(k,12)}${r[k]}</span></div>
  <div><div class="base">Capacidade estimada para nova GD</div><div class="big">${fmt(t.cap)} <small>kW</small></div><div class="base">${esc(LIM[t.lim]||t.lim)}</div></div>
  <dl><dt>Potência do trafo</dt><dd>${fmt(t.kva,1)} kVA</dd><dt>GD já ligada</dt><dd>${fmt(t.gd,1)} kW</dd>
  <dt>Alimentador ${esc(t.alim||"–")}</dt><dd>${fmt(t.capa,0)} kW livres</dd></dl>
  <details class="mais"${detAberto?" open":""}><summary>Mais detalhes</summary>
  <dl><dt>Subestação</dt><dd>${esc(t.sub||"–")}</dd>
  <dt>Unidades geradoras</dt><dd>${t.ugs}</dd><dt>Unidades consumidoras</dt><dd>${t.ucs}</dd>
  <dt>Carga mín. diurna</dt><dd>${fmt(t.cmin,2)} kW</dd><dt>Demanda de pico</dt><dd>${fmt(t.pico,2)} kW</dd>
  <dt>Carregamento do trafo</dt><dd>${fmt(t.car)} %</dd><dt>GD / trafo</dt><dd>${fmt(t.kva?t.gd/t.kva*100:null)} %</dd>
  <dt>Carregamento do alimentador</dt><dd>${fmt(t.ca)} %</dd><dt>GD no alimentador</dt><dd>${fmt(t.gda,0)} kW</dd>
  <dt>Carregamento de GD do alimentador</dt><dd>${fmt(t.cga)} %</dd></dl></details>
  ${t.bt&&!t.bt.km&&!t.bt.np?`<div class="base">Sem rede BT nem GD cadastradas para este trafo na BDGD.</div>`:t.bt?`<div class="base">Rede BT no mapa: ${fmt(t.bt.km,2)} km · ${t.bt.np} poste${t.bt.np===1?"":"s"} com GD (${fmt(t.bt.kw)} kW) <span class="dot" style="display:inline-block;vertical-align:middle;background:#ffcc00"></span></div>`:(META.muns?`<div class="base">Carregando rede BT…</div>`:"")}
  <div class="acoes"><button type="button" data-acao="link">Compartilhar link</button><button type="button" class="ghost" data-acao="pdf">Baixar PDF</button></div>
  <div class="links"><a href="https://www.google.com/maps?q=${t.lat},${t.lon}" target="_blank" rel="noopener">Abrir no Google Maps</a>
  <a class="gsat" href="${gSat(t.lat,t.lon)}" target="_blank" rel="noopener">Satélite no Google</a>
  <span class="base">${t.lat.toFixed(6)}, ${t.lon.toFixed(6)}</span></div></div>`;
}
// link direto para o trafo (#município/trafo/kW) e relatório para imprimir ou salvar em PDF
function linkTrafo(t){const pot=+potEl.value||0;
  return location.origin+location.pathname+"#"+[META.mun||"",t.id,pot>0?pot:""].filter((x,i)=>i<2||x!=="").map(x=>encodeURIComponent(x)).join("/")}
async function compartilhar(t,b){
  const u=linkTrafo(t);
  try{if(navigator.share&&!desk.matches){await navigator.share({title:"Trafo "+t.id,url:u});return}
    await navigator.clipboard.writeText(u);b.textContent="Link copiado!"}
  catch(e){if(e&&e.name==="AbortError")return;
    const a=document.createElement("textarea");a.value=u;document.body.append(a);a.select();
    try{document.execCommand("copy");b.textContent="Link copiado!"}catch(e2){b.textContent="Não consegui copiar"}a.remove()}
  setTimeout(()=>{b.textContent="Compartilhar link"},2500);
}
function relatorio(t){
  const pot=+potEl.value||0,k=classe(t,pot),r=pot>0?ROTP:ROT,lin=(a,b)=>`<tr><th>${a}</th><td>${b}</td></tr>`;
  const w=window.open("","_blank");if(!w)return;
  w.document.write(`<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><title>Trafo ${esc(t.id)} – capacidade de hospedagem</title>
  <style>body{font:14px/1.5 Arial,sans-serif;color:#15211d;margin:32px;max-width:720px}h1{font-size:22px;margin:0 0 4px}
  .s{color:#5d6b66;font-size:12px}table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border-bottom:1px solid #d9e0dd;padding:6px 4px;text-align:left}
  th{font-weight:normal;color:#5d6b66;width:55%}.big{font-size:28px;font-weight:bold}@media print{button{display:none}}</style></head><body>
  <h1>Capacidade de hospedagem de GD · Trafo ${esc(t.id)}</h1>
  <div class="s">${esc(META.munNome||"")} · dados da BDGD de ${esc(META.base)} · emitido em ${new Date().toLocaleDateString("pt-BR")}</div>
  <p>Capacidade estimada para nova GD: <span class="big">${fmt(t.cap)} kW</span><br>${esc(LIM[t.lim]||t.lim)}${pot>0?`<br>Para ${fmt(pot)} kW: <b>${esc(r[k])}</b>`:""}</p>
  <table>${lin("Potência do trafo",fmt(t.kva,1)+" kVA")+lin("GD já ligada",fmt(t.gd,1)+" kW")+lin("Unidades consumidoras · geradoras",t.ucs+" · "+t.ugs)+
    lin("Carga mínima diurna",fmt(t.cmin,2)+" kW")+lin("Demanda de pico",fmt(t.pico,2)+" kW")+lin("Carregamento do trafo",fmt(t.car)+" %")+
    lin("Alimentador · subestação",esc(t.alim||"–")+" · "+esc(t.sub||"–"))+lin("Capacidade p/ nova GD no alimentador",fmt(t.capa,0)+" kW")+
    lin("Carregamento do alimentador",fmt(t.ca)+" %")+lin("GD no alimentador",fmt(t.gda,0)+" kW")+lin("Coordenadas",t.lat.toFixed(6)+", "+t.lon.toFixed(6))}</table>
  <p class="s">Estimativa para triagem a partir de dados públicos da ANEEL (BDGD e base de GD). Não considera proteção nem manobras e não substitui a análise de inversão de fluxo da distribuidora.</p>
  <p class="s">Consulta on-line: ${esc(linkTrafo(t))}</p><button onclick="print()">Imprimir / salvar PDF</button></body></html>`);
  w.document.close();w.focus();setTimeout(()=>{try{w.print()}catch(e){}},300);
}
// ficha flutuando sobre o mapa, ao lado do trafo (no celular ela fica na gaveta)
const desk=matchMedia("(min-width:761px)");
let fichaPop=null,fichaEl=null,fichaFechada=false,fechandoProg=false;
function atualizarFicha(){
  const aberta=fichaPop&&map.hasLayer(fichaPop);
  if(!desk.matches||!sel||fichaFechada){if(aberta){fechandoProg=true;map.closePopup(fichaPop);fechandoProg=false}return}
  if(!fichaPop){
    fichaEl=document.createElement("div");fichaEl.addEventListener("click",clique);
    fichaEl.addEventListener("toggle",e=>{if(e.target.matches("details.mais")){detAberto=e.target.open;fichaPop.update()}},true);
    fichaPop=L.popup({className:"fichaPop",maxWidth:340,minWidth:300,closeOnClick:false,autoPanPaddingTopLeft:[20,70],autoPanPaddingBottomRight:[20,20]});
    fichaPop.on("remove",()=>{if(!fechandoProg)fichaFechada=true});
  }
  // só redesenha (e reposiciona o mapa) quando o conteúdo ou o trafo mudou
  const h=ficha(sel),ll=L.latLng(sel.lat,sel.lon);
  if(!aberta){fichaEl.innerHTML=fichaUlt=h;fichaPop.setLatLng(ll).setContent(fichaEl).openOn(map)}
  else if(h!==fichaUlt||!fichaPop.getLatLng().equals(ll)){fichaEl.innerHTML=fichaUlt=h;fichaPop.setLatLng(ll);fichaPop.update()}
}
let fichaUlt="";
desk.addEventListener?.("change",()=>render());
function dist(a,b,c,d){const R=6371e3,r=Math.PI/180,x=(d-b)*r*Math.cos((a+c)/2*r),y=(c-a)*r;return Math.sqrt(x*x+y*y)}
function proximos(lat,lon,n=6){return visiveis().map(t=>({t,d:dist(lat,lon,t.lat,t.lon)})).sort((a,b)=>a.d-b.d).slice(0,n)}
function listaProx(lat,lon){
  const pot=+potEl.value||0;
  return `<section><h2>${pot>0?"Outras opções perto":"Trafos mais próximos do ponto"}</h2><div class="near">${proximos(lat,lon,7).filter(({t})=>t!==sel).slice(0,6).map(({t,d})=>
   `<button data-id="${esc(t.id)}">${icone(classe(t,pot),16)}<span><b class="id">${esc(t.id)}</b><br><span class="d">${fmt(t.kva,1)} kVA · GD ${fmt(t.gd,1)} kW</span></span><span><b>${fmt(t.cap)}</b> <span class="d">kW</span><br><span class="d">${fmtD(d)}</span></span></button>`).join("")}</div>
   <p class="hint">${filtrando()?"Só trafos que comportam "+fmt(+potEl.value)+" kW. ":""}Distância em linha reta. A conexão segue a rede de BT do trafo que atende o local.</p></section>`;
}
let ponto=null,msg="";
function render(){
  let h="";if(msg)h+=`<p class="msg">${esc(msg)}</p>`;
  if(ponto)h+=veredito();
  if(sel&&!desk.matches)h+=`<section><h2>Transformador</h2>${ficha(sel)}</section>`;
  if(ponto)h+=listaProx(ponto[0],ponto[1]);
  // legenda: no canto do mapa (computador) ou dentro da gaveta (celular, onde o mapa é pequeno)
  const leg=legenda();legCtl.getContainer().innerHTML=leg;
  h+=resumo()+`<section class="legenda soCel">${leg}</section>`+rodape();info.innerHTML=h;
  infoEmp.innerHTML=(empSel?fichaEmp(empSel):"")+resumoEmp();
  infoInv.innerHTML=listaInv();
  atualizarFicha();
}
// cliques nos botões do painel e da ficha: trafo da lista, "ver trafo", compartilhar, PDF
function clique(e){
  const b=e.target.closest("button[data-id],button[data-tr],button[data-acao]");if(!b)return;
  if(b.dataset.acao){if(sel)b.dataset.acao==="pdf"?relatorio(sel):compartilhar(sel,b);return}
  const t=D.find(x=>x.id===(b.dataset.id||b.dataset.tr));if(t){empSel=null;abrir(t)}
}
info.addEventListener("click",clique);infoEmp.addEventListener("click",clique);
function abrir(t,voar=true){
  sel=t;msg="";empSel=null;fichaFechada=false;if(!desk.matches)aba("C");
  try{history.replaceState(null,"","#"+[META.mun||"",t.id].map(encodeURIComponent).join("/"))}catch(e){}
  if(voar)map.setView([t.lat,t.lon],Math.max(map.getZoom(),17),{animate:false});desenhar();render();mostrarRede(t);topoPainel();abrirPainel()}
function marcarPonto(lat,lon,rotulo){
  ponto=[lat,lon];empSel=null;fichaFechada=false;aba("C");if(pino)map.removeLayer(pino);
  pino=L.marker([lat,lon]).addTo(map).bindTooltip(rotulo||"Ponto consultado");
  // o trafo do ponto é o mais próximo, mesmo que não comporte a potência (a resposta diz se cabe)
  const p=maisProximo(lat,lon);sel=p?p.t:null;msg="";map.setView([lat,lon],18,{animate:false});desenhar();render();mostrarRede(sel);abrirPainel();topoPainel();
}
map.on("click",e=>{if(abaAtual==="M")medClique(e.latlng);else pontoGlobal(e.latlng.lat,e.latlng.lng)});
let abaAtual="C";
// Área inteira: troca de município sob demanda (dados/<código>.json)
// Várias distribuidoras (META.dists): cada município tem d (distribuidora) e k (chave única "d:código");
// os dados de cada distribuidora ficam em <p>dados/ (ex.: RGE em dados/, CEEE em ceee/dados/)
let DB=BASE;
const munK=m=>m.k||m.c,distDe=m=>(META.dists||[]).find(d=>d.k===m.d);
function dentro(b,lat,lon){return b&&lat>=b[0]&&lat<=b[2]&&lon>=b[1]&&lon<=b[3]}
function distC(b,lat,lon){return (lat-(b[0]+b[2])/2)**2+(lon-(b[1]+b[3])/2)**2}
async function pontoGlobal(lat,lon,rot){
  if(META.muns&&!dentro(META.bbox,lat,lon)){
    const m=META.muns.filter(x=>dentro(x.b,lat,lon)).sort((x,y)=>(x.d!==META.dist)-(y.d!==META.dist)||distC(x.b,lat,lon)-distC(y.b,lat,lon))[0];
    if(m&&munK(m)!==META.munK)await carregarMun(munK(m),false);
  }
  marcarPonto(lat,lon,rot);
}
async function carregarMun(c,aj=true){
  const m=META.muns.find(x=>munK(x)===c)||META.muns.find(x=>x.c===c)||META.muns[0];
  const dd=distDe(m),db=BASE+(dd?dd.p:"");
  msg="Carregando "+m.n+"…";render();
  try{const r=await fetch(`${db}dados/${m.c}.json`);if(!r.ok)throw 0;D=await r.json()}
  catch(e){msg="Não consegui carregar os dados de "+m.n+".";render();return}
  if(dd&&dd.k!==META.dist){   // trocou de distribuidora: limpa o que é dela
    if(!META.dists){INV=null;invErro=false;invVis=[]}for(const o of [btCache,mtCache,empCache])for(const k in o)delete o[k];$("empAtv").dataset.mun="";
    const op=!META.dists&&$("invOnde").querySelector('option[value="t"]');if(op)op.textContent="Toda a área da "+dd.n}
  DB=db;META.dist=dd?dd.k:null;META.munK=munK(m);if(dd)META.titulo=dd.n;
  if(distEl.value&&dd&&distEl.value!==dd.k){distEl.value=dd.k;listarMuns()}
  META.mun=m.c;META.bbox=m.b;META.munNome=m.n+", RS";munEl.value=munK(m);
  const mb=document.getElementById("munBusca");if(mb&&document.activeElement!==mb)mb.value=(munEl.selectedOptions[0]||{}).textContent||m.n;
  document.getElementById("titulo").textContent=m.n;document.title=(BETA?"[BETA] ":"")+"Capacidade de Hospedagem "+m.n;
  sel=null;ponto=null;msg="";if(pino){map.removeLayer(pino);pino=null}
  empSel=null;preparar();preencherAlim();desenhar();render();if(aj)ajustar();desenharEmp();
  if(typeof abaAtual!=="undefined"&&abaAtual==="I"&&!INV)ligarInv();
  try{history.replaceState(null,"","#"+munK(m))}catch(e){}
}
// ---- Prospecção de empresas (dados/emp/<município>.json, sob demanda)
// linha: [lat,lon,kWh/mês,kWp,divisão,classe,gd,rede,endereço,bairro,grupo,mt,DIC,trafo,alimentador,demanda,CNAE]
const empLayer=L.layerGroup().addTo(map),empCache={};let empSel=null,empVis=[];
const GDR=["sem GD","talvez (GD no mesmo ponto de conexão)","provável","sim"],RED=["sem dado","não (precisa de estudo)","parcial","sim"];
const $=id=>document.getElementById(id);
async function dadosEmp(){
  const c=META.mun;if(!c)return null;
  if(!(c in empCache)){try{const r=await fetch(`${DB}dados/emp/${c}.json`);empCache[c]=r.ok?await r.json():{a:{},e:[]}}catch(e){empCache[c]={a:{},e:[]}}}
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
// ---- Usinas de investimento (dados/invest.json, toda a área): trafos com 1 UC cujo alimentador tem folga p/ nova GD
// linha: {id,lat,lon,kva,gd,cmin,capa,alim,sub,m (código IBGE)}
let INV=null,invVis=[],invN=30,invErro=false,ALIM=null;
// t.d = distribuidora do local (página com várias distribuidoras carrega os locais de todas)
const nomeMun=t=>((META.muns||[]).find(m=>m.c===t.m&&(!t.d||m.d===t.d))||{n:t.m}).n;
const nomeDist=k=>((META.dists||[]).find(d=>d.k===k)||{n:k||""}).n;
// "m" = município aberto; "t" = toda a área; "t:RGE" = toda a área de uma distribuidora
function invOnde(){const v=$("invOnde").value;return {todo:v[0]==="t",dist:v.slice(2),rotulo:$("invOnde").selectedOptions[0].textContent}}
async function ligarInv(){
  if(!INV&&!invErro){infoInv.innerHTML=`<p class="hint">Carregando os locais…</p>`;
    try{const fontes=META.dists||[{k:"",p:""}];
      const partes=await Promise.all(fontes.map(async ds=>{const r=await fetch(`${BASE}${ds.p}dados/invest.json`);if(!r.ok)throw 0;
        const l=await r.json();l.forEach(t=>{t.d=ds.k});return l}));
      INV=partes.flat();
      const al=await Promise.all(fontes.map(async ds=>{try{const r=await fetch(`${BASE}${ds.p}dados/alim.json`);if(!r.ok)return [];
        const l=await r.json();l.forEach(a=>{a.d=ds.k;a.p=ds.p});return l}catch(e){return []}}));
      ALIM=al.flat();if(invOnde().todo){const b=META.bboxAll;map.fitBounds([[b[0],b[1]],[b[2],b[3]]])}
      // faixas de potência (kVA) presentes nos locais, para o filtro "Trafo de … até …"
      const vs=[...new Set(INV.map(t=>t.kva).filter(v=>v!=null))].sort((a,b)=>a-b);
      for(const el of [$("invKvaMin"),$("invKvaMax")])vs.forEach(v=>{const o=document.createElement("option");o.value=v;o.textContent=fmtK(v);el.append(o)});
      INV.forEach(t=>{const inv=t.cmin==null?null:t.cmin*META.fs-t.gd,lim=t.kva==null?null:t.kva-t.gd;
        t.cap=(inv==null&&lim==null)?null:Math.max(0,Math.min(inv??Infinity,lim??Infinity))})}
    catch(e){invErro=true}}
  desenhar();render();
}
// sem potência pretendida: qualquer folga (> 0); com potência: folga ≥ potência. Ordem: maior folga do alimentador
function invFiltro(){
  const pot=+potEl.value||0,tr=$("invTr").checked,tri=$("invTri").checked,o=invOnde(),ok=v=>v!=null&&(pot>0?v>=pot:v>0);
  const onde=t=>o.todo?(!o.dist||t.d===o.dist):(t.m===META.mun&&(!META.dist||t.d===META.dist));
  const k0=$("invKvaMin").value===""?null:+$("invKvaMin").value,k1=$("invKvaMax").value===""?null:+$("invKvaMax").value;
  const faixa=t=>(k0==null||(t.kva!=null&&t.kva>=k0))&&(k1==null||(t.kva!=null&&t.kva<=k1));
  return INV.filter(t=>onde(t)&&faixa(t)&&ok(t.capa)&&(!tr||ok(t.cap))&&(!tri||t.f===3)).sort((a,b)=>b.capa-a.capa||(b.cap??-1)-(a.cap??-1));
}
async function abrirInv(t){
  if(t.m!==META.mun||(t.d&&t.d!==META.dist))await carregarMun((t.d?t.d+":":"")+t.m,false);
  const x=D.find(x=>x.id===t.id);if(x)abrir(x);
}
// ---- Alimentadores no mapa (dados/alim.json de cada distribuidora): longe = um círculo por alimentador
// no centro dos trafos dele; perto (zoom ≥ 12) = traçado da rede MT. Verde = folga p/ nova GD (sem inversão).
map.createPane("alims");map.getPane("alims").style.zIndex=350;
const rAlim=L.canvas({pane:"alims"}),camAlim=L.layerGroup().addTo(map),mtTodos={};let geracaoAlim=0;
function alimOk(a){const pot=+potEl.value||0;return a.capa!=null&&(pot>0?a.capa>=pot:a.capa>0)}
function alimEscopo(){
  if(!ALIM)return [];const o=invOnde();
  if(o.todo)return ALIM.filter(a=>!o.dist||a.d===o.dist);
  const daqui=new Set(D.map(t=>t.alim));return ALIM.filter(a=>daqui.has(a.a)&&(!META.dist||a.d===META.dist));
}
function dicaAlim(a){
  return `<b>Alimentador ${esc(a.a)}</b>${META.dists?" · "+esc(nomeDist(a.d)):""}${a.s?" · SE "+esc(a.s):""}<br>
  ${a.capa==null?"Sem dado de carga":alimOk(a)?`Folga p/ nova GD: <b>${fmt(a.capa,0)} kW</b> (sem inversão de fluxo)`:`<b>Sem folga</b> p/ ${(+potEl.value||0)>0?fmt(+potEl.value)+" kW":"nova GD"} (inversão de fluxo)`}<br>
  GD ligada: ${fmt(a.gda,0)} kW · carregamento ${fmt(a.ca)} %<br>${a.n.toLocaleString("pt-BR")} trafos · ${a.i} com 1 UC (${a.i3} trifásicos)`;
}
async function desenharAlim(){
  const g=++geracaoAlim;camAlim.clearLayers();
  const modo=$("invAlim").value;if(!ALIM||!modo)return;
  const lista=alimEscopo().filter(a=>modo==="todos"?a.capa!=null:alimOk(a));
  const cor=a=>alimOk(a)?"#16a34a":"#dc2626";
  if(map.getZoom()<12){
    lista.forEach(a=>{const m=L.circleMarker([a.lat,a.lon],{renderer:rAlim,radius:Math.min(14,4+Math.sqrt(Math.max(a.capa||0,0))/6),
      color:"#ffffff",weight:1,fillColor:cor(a),fillOpacity:.75});
      m.bindTooltip(()=>dicaAlim(a),{direction:"top",offset:[0,-6]});
      m.on("click",e=>{L.DomEvent.stopPropagation(e);map.fitBounds([[a.b[0],a.b[1]],[a.b[2],a.b[3]]],{padding:[20,20]})});camAlim.addLayer(m)});
    return;
  }
  const v=map.getBounds(),perto=lista.filter(a=>a.b[0]<=v.getNorth()&&a.b[2]>=v.getSouth()&&a.b[1]<=v.getEast()&&a.b[3]>=v.getWest()).slice(0,60);
  await Promise.all(perto.map(async a=>{const f=a.p+String(a.a).replace(/[^\w.-]/g,"_");
    if(!(f in mtTodos))try{const r=await fetch(`${BASE}${a.p}dados/mt/${String(a.a).replace(/[^\w.-]/g,"_")}.json`);mtTodos[f]=r.ok?await r.json():null}catch(e){mtTodos[f]=null}}));
  if(g!==geracaoAlim)return;
  perto.forEach(a=>{const d=mtTodos[a.p+String(a.a).replace(/[^\w.-]/g,"_")];if(!d)return;
    linhas(d,{renderer:rAlim,color:cor(a),weight:3,opacity:.7},camAlim).forEach(l=>l.bindTooltip(()=>dicaAlim(a),{sticky:true}))});
}
// de longe só os alimentadores; os locais (círculos azuis) aparecem ao aproximar
const ZOOM_LOCAIS=11;let pertoAnt=null;
map.on("zoomend moveend",()=>{if(!ALIM)return;const pz=map.getZoom()>=ZOOM_LOCAIS;
  if(pz!==pertoAnt){pertoAnt=pz;desenhar()}else desenharAlim()});
function dicaInv(t){
  return `<b>Usina de investimento · trafo ${esc(t.id)}</b><br>${esc(nomeMun(t))}${t.d&&META.dists?" ("+esc(nomeDist(t.d))+")":""} · ${fmt(t.kva,1)} kVA${t.f?" "+(t.f===3?"trifásico":t.f===2?"bifásico":"monofásico"):""} · 1 UC<br>
  Alimentador ${esc(t.alim||"–")}: folga <b>${fmt(t.capa,0)} kW</b><br>Trafo comporta: ${fmt(t.cap)} kW`;
}
function listaInv(){
  if(!META.muns)return "";
  if(invErro)return `<p class="msg">Não consegui carregar a lista de locais. Tente recarregar a página.</p>`;
  if(!INV)return "";
  const o=invOnde(),todo=o.todo;
  if(!todo&&!META.mun)return `<p class="hint">Carregando o município…</p>`;
  const pot=+potEl.value||0,tr=$("invTr").checked,na=new Set(invVis.map(t=>t.d+"|"+t.alim)).size;
  return `<section><h2>${todo?esc(o.rotulo):esc(META.munNome.split(",")[0])}</h2>
  <div class="stats"><div class="stat"><b>${invVis.length.toLocaleString("pt-BR")}</b><span>locais</span></div>
  <div class="stat"><b>${na.toLocaleString("pt-BR")}</b><span>alimentadores</span></div>
  ${ALIM&&todo?(()=>{const l=alimEscopo(),k=l.filter(alimOk).length;return `<div class="stat"><b>${k.toLocaleString("pt-BR")}</b><span>de ${l.length.toLocaleString("pt-BR")} alim. sem inversão</span></div>`})():""}
  <div class="stat"><b>${invVis.length?fmt(invVis[0].capa/1000,2):"–"}</b><span>MW maior folga</span></div></div>
  <p class="hint">${pot>0?`Alimentador com folga ≥ ${fmt(pot)} kW`:"Qualquer folga no alimentador"}${tr?" e no trafo":""}${$("invTri").checked?"; só trafo trifásico":""}${($("invKvaMin").value||$("invKvaMax").value)?`; trafo de ${$("invKvaMin").value?fmtK(+$("invKvaMin").value):"qualquer"} a ${$("invKvaMax").value?fmtK(+$("invKvaMax").value):"qualquer"} kVA`:""}. Locais do mesmo alimentador disputam a mesma folga.
  ${tr?"":"O trafo de um só consumidor costuma ser pequeno: a usina em geral exige trocar ou ampliar o trafo. "}${$("invMapa").checked?($("invAlim").value?"Círculos azuis no mapa ao aproximar.":"Círculos azuis no mapa."):""}</p>
  ${$("invAlim").value?`<p class="hint"><b style="color:#16a34a">Verde</b>: alimentador sem inversão de fluxo (círculo maior = mais folga)${$("invAlim").value==="todos"?"; <b style=\"color:#dc2626\">vermelho</b>: sem folga":""}. Clique para aproximar; de perto aparece o traçado da rede de cada alimentador.</p>`:""}
  ${invVis.length?"":`<p class="hint">Nenhum local ${todo?"na área":"neste município"} com esses critérios.${todo?"":" Escolha uma opção de área inteira em “Onde”."}</p>`}
  <div class="near">${invVis.slice(0,invN).map(t=>`<button data-inv="${esc(t.id)}" data-m="${esc(t.m)}" data-d="${esc(t.d||"")}"><span class="dot inv"></span><span><b class="id">${esc(t.id)}</b><br><span class="d">${todo?esc(nomeMun(t))+(META.dists&&!o.dist?" ("+esc(nomeDist(t.d))+")":"")+" · ":""}${esc(t.alim||"–")} · ${fmtK(t.kva)} kVA${t.f?" · "+(t.f===3?"trif.":t.f===2?"bif.":"monof."):""}</span></span><span><b>${fmt(t.capa,0)}</b> <span class="d">kW alim.</span><br><span class="d">trafo ${fmt(t.cap,0)} kW</span></span></button>`).join("")}</div>
  ${invVis.length>invN?`<button type="button" class="ghost" data-mais="1">Mostrar mais (${(invVis.length-invN).toLocaleString("pt-BR")} restantes)</button>`:""}</section>`;
}
infoInv.addEventListener("click",e=>{
  const b=e.target.closest("button[data-inv],button[data-mais]");if(!b)return;
  if(b.dataset.mais){invN+=30;render();return}
  const t=INV.find(t=>t.id===b.dataset.inv&&t.m===b.dataset.m&&(t.d||"")===b.dataset.d);if(t)abrirInv(t);
});
function baixarInv(){
  const q=v=>`"${String(v??"").replace(/"/g,'""')}"`,n=v=>v==null?"":String(Math.round(v*10)/10).replace(".",",");
  const cab=["Trafo","Município","Distribuidora","Alimentador","Subestação","Fases","Potência do trafo (kVA)","GD já ligada (kW)","Folga do alimentador p/ nova GD (kW)","Trafo comporta (kW)","Latitude","Longitude","Google Maps"];
  const lin=invVis.map(t=>[t.id,nomeMun(t),nomeDist(t.d)||META.titulo,t.alim,t.sub,t.f||"",n(t.kva),n(t.gd),n(t.capa),n(t.cap),t.lat,t.lon,`https://www.google.com/maps?q=${t.lat},${t.lon}`].map(q).join(";"));
  const blob=new Blob(["\ufeff"+[cab.map(q).join(";"),...lin].join("\r\n")],{type:"text/csv;charset=utf-8"});
  const o=invOnde(),onde=(o.todo?(o.dist?nomeDist(o.dist):"RS"):META.munNome.split(",")[0]).replace(/\W+/g,"_");
  const l=document.createElement("a");l.href=URL.createObjectURL(blob);l.download=`usinas_investimento_${onde}.csv`;l.click();setTimeout(()=>URL.revokeObjectURL(l.href),2000);
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
// computador: painel no topo; celular: rola a gaveta até a resposta/ficha da aba aberta
function topoPainel(){
  if(desk.matches){side.scrollTop=0;return}
  const a=document.querySelector(".aba:not([hidden]) .scroll");if(!a)return;
  side.scrollTop+=a.getBoundingClientRect().top-side.getBoundingClientRect().top-8;
}
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
  <a class="gsat" href="${gSat(x[0],x[1])}" target="_blank" rel="noopener">Satélite no Google</a>
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
  const f=[];
  if(alimEl.value)f.push(alimEl.value);
  if(kvaMinEl.value||kvaMaxEl.value)f.push(`${kvaMinEl.value?fmtK(+kvaMinEl.value):"…"}–${kvaMaxEl.value?fmtK(+kvaMaxEl.value):"…"} kVA`);
  if(uc1El.checked)f.push("1 UC");if(document.getElementById("alimOk").checked)f.push("alim. comporta");
  document.getElementById("ftrN").textContent=f.length?"· "+f.join(" · "):"";
}
function aoMudarPot(){resumoFiltros();filtraRow.hidden=!((+potEl.value||0)>0);if(sel&&!base().includes(sel)){const p=ponto&&maisProximo(ponto[0],ponto[1]);sel=p?p.t:null}desenhar();render()}
potEl.addEventListener("input",aoMudarPot);uc1El.addEventListener("change",aoMudarPot);
kvaMinEl.addEventListener("change",aoMudarPot);kvaMaxEl.addEventListener("change",aoMudarPot);
document.getElementById("alimOk").addEventListener("change",aoMudarPot);filtraEl.addEventListener("change",aoMudarPot);
alimEl.addEventListener("change",()=>{sel=null;resumoFiltros();desenhar();render();ajustar()});
const side=document.getElementById("side"),tog=document.getElementById("tog");
tog.onclick=()=>{side.classList.toggle("min");tog.setAttribute("aria-expanded",!side.classList.contains("min"));setTimeout(()=>map.invalidateSize(),50)};
function abrirPainel(){if(side.classList.contains("min")){side.classList.remove("min");setTimeout(()=>map.invalidateSize(),50)}}
function ajustar(){const v=visiveis();if(v.length)map.fitBounds(v.map(t=>[t.lat,t.lon]),{padding:[20,20]})}
matchMedia("(prefers-color-scheme:dark)").addEventListener?.("change",()=>desenhar());
// ---- Medir telhado: contornos de prédios (dados/edif/14/<x>/<y>.json) ou desenho à mão
const med={cache:{},pts:[],fonte:"",modo:"toque",editando:false,camada:L.layerGroup().addTo(map),
  marcadores:[],estavaSat:false,trafosOcultos:false,empOcultas:false};
const FONTES=["OpenStreetMap","Google Open Buildings (detectado por IA)","Microsoft (detectado por IA)"];
const medAtrib="Prédios © OpenStreetMap contributors, Overture Maps";
function medEntrar(){
  med.estavaSat=map.hasLayer(sat)||(gTiles&&map.hasLayer(gTiles));
  if(!med.estavaSat){map.removeLayer(osm);(gTiles||sat).addTo(map);map.fire("baselayerchange")}
  if(map.hasLayer(capa)){map.removeLayer(capa);med.trafosOcultos=true}
  if(map.hasLayer(empLayer)){map.removeLayer(empLayer);med.empOcultas=true}
  map.attributionControl.addAttribution(medAtrib);
  if(map.getZoom()<18&&map.getZoom()>=14)map.setZoom(18);
  medRender();
}
function medSair(){
  if(med.trafosOcultos&&!$("empHideTr").checked)capa.addTo(map);
  if(med.empOcultas)empLayer.addTo(map);
  med.trafosOcultos=med.empOcultas=false;
  map.attributionControl.removeAttribution(medAtrib);
}
function tileZ14(lat,lon){const n=16384,la=lat*Math.PI/180;
  return [Math.floor((lon+180)/360*n),Math.floor((1-Math.log(Math.tan(la)+1/Math.cos(la))/Math.PI)/2*n)]}
async function edificios(lat,lon){
  const [x,y]=tileZ14(lat,lon),k=x+"/"+y;
  if(!(k in med.cache)){   // contornos de todas as distribuidoras (a medição não depende do município)
    const lst=await Promise.all((META.dists||[{p:""}]).map(async ds=>{try{const r=await fetch(`${BASE}${ds.p}dados/edif/14/${k}.json`);
      if(!r.ok)return [];const d=await r.json();
      return d.b.map(a=>{let px=a[1]+d.o[0],py=a[2]+d.o[1];const p=[[py/1e6,px/1e6]];
        for(let i=3;i<a.length;i+=2){px+=a[i];py+=a[i+1];p.push([py/1e6,px/1e6])}return {f:a[0],p}})}catch(e){return []}}));
    med.cache[k]=lst.flat()}
  return med.cache[k];
}
// geometria em metros (projeção local)
function proj(pts){const la0=pts[0][0],lo0=pts[0][1],kx=111320*Math.cos(la0*Math.PI/180),ky=110540;
  return pts.map(([la,lo])=>[(lo-lo0)*kx,(la-la0)*ky])}
function dentroPoly(pt,poly){let c=false;for(let i=0,j=poly.length-1;i<poly.length;j=i++){const [yi,xi]=poly[i],[yj,xj]=poly[j];
  if(((yi>pt[0])!==(yj>pt[0]))&&(pt[1]<(xj-xi)*(pt[0]-yi)/(yj-yi)+xi))c=!c}return c}
function distPoly(pt,poly){const q=proj([pt,...poly]),[px,py]=q[0];let d=1e9;
  for(let i=1;i<q.length;i++){const [ax,ay]=q[i],[bx,by]=q[i%(q.length-1)+1],dx=bx-ax,dy=by-ay,l=dx*dx+dy*dy;
    const t=l?Math.max(0,Math.min(1,((px-ax)*dx+(py-ay)*dy)/l)):0;d=Math.min(d,Math.hypot(px-ax-t*dx,py-ay-t*dy))}return d}
function medidas(pts){
  const q=proj(pts),n=q.length;let a=0,per=0;const lados=[];
  for(let i=0;i<n;i++){const [x1,y1]=q[i],[x2,y2]=q[(i+1)%n];a+=x1*y2-x2*y1;const l=Math.hypot(x2-x1,y2-y1);per+=l;lados.push(l)}
  // menor retângulo que envolve o contorno (testa a direção de cada lado)
  let best=null;for(let i=0;i<n;i++){const [x1,y1]=q[i],[x2,y2]=q[(i+1)%n],L=Math.hypot(x2-x1,y2-y1);if(L<.01)continue;
    const ux=(x2-x1)/L,uy=(y2-y1)/L;let a0=1e9,a1=-1e9,b0=1e9,b1=-1e9;
    q.forEach(([x,y])=>{const s=x*ux+y*uy,t=-x*uy+y*ux;a0=Math.min(a0,s);a1=Math.max(a1,s);b0=Math.min(b0,t);b1=Math.max(b1,t)});
    const A=(a1-a0)*(b1-b0);if(!best||A<best.A)best={A,c:Math.max(a1-a0,b1-b0),l:Math.min(a1-a0,b1-b0)}}
  return {area:Math.abs(a)/2,per,lados,ret:best}
}
async function medClique(ll){
  if(med.modo==="desenho"){med.pts.push([ll.lat,ll.lng]);med.fonte="desenho";medDesenhar();medRender();return}
  if(map.getZoom()<16){medMsg("Aproxime mais o mapa (zoom de rua) e toque no telhado.");return}
  medMsg("Procurando o contorno do telhado…");
  const lista=await edificios(ll.lat,ll.lng),pt=[ll.lat,ll.lng];
  let e=lista.find(b=>dentroPoly(pt,b.p));
  if(!e){let dm=8;lista.forEach(b=>{const d=distPoly(pt,b.p);if(d<dm){dm=d;e=b}})}
  if(!e){med.pts=[];medDesenhar();medMsg("Não achei contorno de prédio neste ponto. Use \"Desenhar contorno\" e marque os cantos do telhado na imagem.");return}
  med.pts=e.p.map(p=>[...p]);med.fonte=FONTES[e.f]||"";med.editando=false;medDesenhar();medRender();
}
function medMsg(t){$("infoMed").innerHTML=`<p class="msg">${esc(t)}</p>`}
function medDesenhar(){
  med.camada.clearLayers();med.marcadores=[];
  if(!med.pts.length)return;
  const fechado=med.pts.length>=3;
  med.forma=(fechado?L.polygon(med.pts,{color:"#f2b400",weight:3,fillColor:"#f2b400",fillOpacity:.25,interactive:false,renderer:rTopo})
          :L.polyline(med.pts,{color:"#f2b400",weight:3,dashArray:"6 4",interactive:false,renderer:rTopo})).addTo(med.camada);
  if(fechado){const m=medidas(med.pts);const n=med.pts.length;
    if(n<=40)med.pts.forEach((p,i)=>{const q=med.pts[(i+1)%n];if(m.lados[i]<1)return;
      L.tooltip({permanent:true,direction:"center",className:"lado"}).setLatLng([(p[0]+q[0])/2,(p[1]+q[1])/2])
        .setContent(fmt(m.lados[i],1)+" m").addTo(med.camada)})}
  if(med.modo==="desenho"||med.editando)med.pts.forEach((p,i)=>{
    const mk=L.marker(p,{draggable:true,icon:L.divIcon({className:"vert",iconSize:[14,14]})}).addTo(med.camada);
    mk.on("drag",ev=>{const g=ev.target.getLatLng();med.pts[i]=[g.lat,g.lng];
      if(med.forma)med.forma.setLatLngs(med.pts)});
    mk.on("dragend",()=>{medDesenhar();medRender()});
    mk.on("click",ev=>{L.DomEvent.stopPropagation(ev);if(med.modo==="desenho"&&i===0&&med.pts.length>=3)medModo("toque",true)});
    med.marcadores.push(mk)});
}
function medModo(m,manter){
  med.modo=m;$("medToque").setAttribute("aria-pressed",m==="toque");$("medDesenho").setAttribute("aria-pressed",m==="desenho");
  $("medDica").textContent=m==="desenho"?"Toque nos cantos do telhado, em sequência. Para terminar, toque no primeiro canto ou em \"Tocar no telhado\". Arraste um canto para ajustar."
    :"Toque num telhado no mapa: o contorno aparece sozinho. Se não aparecer, ou para medir só uma parte, use \"Desenhar contorno\".";
  if(m==="desenho"&&!manter){med.pts=[];med.fonte="desenho";med.editando=false}
  medDesenhar();medRender();
}
function medRender(){
  const el=$("infoMed");
  if(med.pts.length<3){if(med.modo==="desenho")el.innerHTML=`<p class="hint">${med.pts.length} de pelo menos 3 cantos marcados.</p>`;
    else if(!med.pts.length)el.innerHTML="";return}
  const m=medidas(med.pts),inc=+$("medInc").value||0,uso=(+$("medUso").value||0)/100,
    real=m.area/Math.cos(inc*Math.PI/180),aMod=(+$("medC").value||0)*(+$("medL").value||0),w=+$("medW").value||0,
    nMod=aMod>0?Math.floor(real*uso/aMod):0,kwp=nMod*w/1000;
  el.innerHTML=`<section><h2>Telhado</h2><div class="card">
   <div><div class="base">Área do telhado (com inclinação de ${inc}°)</div><div class="medGrande">${fmt(real,1)} <small>m²</small></div></div>
   <dl><dt>Área vista de cima</dt><dd>${fmt(m.area,1)} m²</dd><dt>Perímetro</dt><dd>${fmt(m.per,1)} m</dd>
   <dt>Medidas (retângulo envolvente)</dt><dd>${fmt(m.ret.c,1)} × ${fmt(m.ret.l,1)} m</dd>
   <dt>Lados</dt><dd>${m.lados.length<=12?m.lados.map(l=>fmt(l,1)).join(" · ")+" m":m.lados.length+" lados"}</dd>
   <dt>Contorno</dt><dd>${esc(med.fonte==="desenho"?"desenhado por você":med.fonte)}</dd></dl></div></section>
   <section><h2>Sistema que cabe</h2><div class="card">
   <div class="medGrande">${fmt(kwp,2)} <small>kWp</small></div>
   <dl><dt>Módulos de ${fmt(w,0)} W</dt><dd>${nMod}</dd><dt>Área usada (${fmt(uso*100,0)}%)</dt><dd>${fmt(real*uso,1)} m²</dd>
   <dt>Geração estimada</dt><dd>~${fmt(kwp*1150/12,0)} kWh/mês</dd></dl>
   ${kwp>75?`<p class="hint">Acima de 75 kW é minigeração: a conexão exige estudo da distribuidora e a potência fica limitada pela demanda contratada.</p>`:""}
   <div class="acoes"><button type="button" id="medConsulta"${kwp>0?"":" disabled"}>Consultar a rede com ${fmt(kwp,2)} kW</button>
   <button type="button" class="ghost" id="medEdita">${med.editando||med.modo==="desenho"?"Parar de editar":"Ajustar cantos"}</button>
   <button type="button" class="ghost" id="medCopia">Copiar medidas</button></div>
   <p class="hint">O aproveitamento desconta afastamentos, sombras e águas viradas para o sul. Geração pela média do RS (~1.150 kWh/kWp por ano); a orientação real do telhado muda esse valor.</p></div></section>`;
  $("medEdita").onclick=()=>{if(med.modo==="desenho")medModo("toque",true);else{med.editando=!med.editando;medDesenhar();medRender()}};
  $("medCopia").onclick=()=>{const t=`Telhado: ${fmt(real,1)} m² (inclinação ${inc}°), vista de cima ${fmt(m.area,1)} m², perímetro ${fmt(m.per,1)} m, ${fmt(m.ret.c,1)} × ${fmt(m.ret.l,1)} m. Cabem ${nMod} módulos de ${w} W = ${fmt(kwp,2)} kWp (~${fmt(kwp*1150/12,0)} kWh/mês).`;
    navigator.clipboard?.writeText(t).then(()=>{$("medCopia").textContent="Copiado!"},()=>{})};
  $("medConsulta").onclick=()=>{const c=med.pts.reduce((s,p)=>[s[0]+p[0]/med.pts.length,s[1]+p[1]/med.pts.length],[0,0]);
    potEl.value=Math.round(kwp*100)/100;aba("C");filtraRow.hidden=false;pontoGlobal(c[0],c[1],"Telhado medido")};
}
["medInc","medUso","medW","medC","medL"].forEach(i=>$(i).addEventListener("input",medRender));
$("medToque").onclick=()=>medModo("toque",true);$("medDesenho").onclick=()=>medModo("desenho");
$("medLimpa").onclick=()=>{med.pts=[];med.editando=false;medDesenhar();medRender();medModo(med.modo,true)};
// abas do painel
let empMexeu=false;
function aba(k){
  const antes=abaAtual;abaAtual=k;
  ["C","P","I","M","S"].forEach(x=>{$("aba"+x).hidden=x!==k;$("tab"+x).setAttribute("aria-selected",x===k)});
  if(k==="M"&&antes!=="M")medEntrar();else if(antes==="M"&&k!=="M")medSair();
  // na primeira vez que abre Prospectar, já liga as empresas no mapa
  if(k==="P"&&!empMexeu&&!$("empOn").checked&&META.muns){empMexeu=true;$("empOn").checked=true;desenharEmp()}
  if(k==="I"&&!INV&&!invErro&&META.muns)ligarInv();
}
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>aba(b.dataset.tab));
const munEl=document.getElementById("mun"),distEl=document.getElementById("dist");
// lista de municípios conforme o filtro de distribuidora ("" = todas); nomes repetidos levam a distribuidora
function listarMuns(){
  const f=distEl.value,ant=munEl.value,lista=META.muns.filter(m=>!f||m.d===f),vis=new Set(),dup=new Set();
  lista.forEach(m=>{if(vis.has(m.c))dup.add(m.c);vis.add(m.c)});
  munEl.innerHTML="";lista.forEach(m=>{const o=document.createElement("option");o.value=munK(m);
    o.textContent=m.n+(dup.has(m.c)?` (${(distDe(m)||{}).n||m.d})`:"");munEl.append(o)});
  if([...munEl.options].some(o=>o.value===ant))munEl.value=ant;
}
// busca de cidade: ao digitar, mostra embaixo só as parecidas (sem acento; as que começam com o texto primeiro)
const munBusca=$("munBusca"),munLista=$("munLista");
const semAc=t=>t.normalize("NFD").replace(/[\u0300-\u036f]/g,"").toLowerCase();
let munIdx=-1;
const nomeAtual=()=>(munEl.selectedOptions[0]||{}).textContent||"";
function mostrarMuns(){
  const q=semAc(munBusca.value.trim());
  let ops=[...munEl.options].map(o=>({k:o.value,t:o.textContent,s:semAc(o.textContent)}));
  if(q)ops=ops.filter(o=>o.s.includes(q)).sort((a,b)=>(b.s.startsWith(q)-a.s.startsWith(q))||a.s.localeCompare(b.s));
  const tot=ops.length;ops=ops.slice(0,60);munIdx=ops.length?0:-1;
  const marca=o=>{if(!q)return esc(o.t);const i=o.s.indexOf(q);return esc(o.t.slice(0,i))+"<mark>"+esc(o.t.slice(i,i+q.length))+"</mark>"+esc(o.t.slice(i+q.length))};
  munLista.innerHTML=ops.length?ops.map((o,i)=>`<li role="option" id="munOp${i}" data-k="${esc(o.k)}" aria-selected="${i===0}">${marca(o)}</li>`).join("")
    +(tot>ops.length?`<li class="vazio">… e mais ${tot-ops.length}. Continue digitando.</li>`:""):`<li class="vazio">Nenhuma cidade encontrada</li>`;
  munLista.hidden=false;munBusca.setAttribute("aria-expanded","true");
}
function fecharMuns(){munLista.hidden=true;munBusca.setAttribute("aria-expanded","false")}
function escolherMun(k){fecharMuns();if(!k)return;munEl.value=k;munBusca.value=nomeAtual();munBusca.blur();carregarMun(k)}
munBusca.addEventListener("focus",()=>{munBusca.value="";mostrarMuns()});
munBusca.addEventListener("input",mostrarMuns);
munBusca.addEventListener("keydown",e=>{const li=[...munLista.querySelectorAll("li[data-k]")];
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){e.preventDefault();if(munLista.hidden)mostrarMuns();if(!li.length)return;
    munIdx=(munIdx+(e.key==="ArrowDown"?1:-1)+li.length)%li.length;li.forEach((x,i)=>x.setAttribute("aria-selected",i===munIdx));li[munIdx].scrollIntoView({block:"nearest"})}
  else if(e.key==="Enter"){e.preventDefault();if(li[munIdx])escolherMun(li[munIdx].dataset.k)}
  else if(e.key==="Escape"){fecharMuns();munBusca.value=nomeAtual();munBusca.blur()}});
munLista.addEventListener("mousedown",e=>{e.preventDefault();const li=e.target.closest("li[data-k]");if(li)escolherMun(li.dataset.k)});
munBusca.addEventListener("blur",()=>setTimeout(()=>{fecharMuns();munBusca.value=nomeAtual()},150));
// link direto: #<município>/<trafo>/<kW>
const [hMun,hTr,hKw]=location.hash.slice(1).split("/").map(s=>{try{return decodeURIComponent(s)}catch(e){return s}});
if(hKw&&+hKw>0){potEl.value=+hKw;filtraRow.hidden=false}
if(META.muns){
  $("tabP").hidden=false;$("tabI").hidden=false;$("btInv").hidden=false;
  $("btInv").onclick=()=>{aba("I");abrirPainel();topoPainel()};
  ["invTr","invTri","invAlim","invMapa","invKvaMin","invKvaMax"].forEach(i=>$(i).addEventListener("change",()=>{invN=30;desenhar();render()}));
  $("invOnde").addEventListener("change",()=>{invN=30;desenhar();render();if(invVis.length)map.fitBounds(invVis.map(t=>[t.lat,t.lon]),{padding:[20,20],maxZoom:15})});
  $("invCsv").addEventListener("click",()=>{if(INV)baixarInv();else ligarInv().then(()=>{if(INV)baixarInv()})});$("empOn").addEventListener("change",()=>{empMexeu=true});
  ["empOn","empAtv","empTen","empGd","empRede"].forEach(i=>$(i).addEventListener("change",()=>{empSel=null;desenharEmp()}));
  $("empKwh").addEventListener("input",()=>{empSel=null;desenharEmp()});
  $("empHideTr").addEventListener("change",()=>{if($("empHideTr").checked)map.removeLayer(capa);else capa.addTo(map)});
  $("empCsv").addEventListener("click",()=>{if(!$("empOn").checked){$("empOn").checked=true;desenharEmp().then(baixarCsv)}else baixarCsv()});
  document.getElementById("munRow").style.display="";
  if(META.dists){$("distRow").style.display="";
    $("invOnde").innerHTML='<option value="m">Município selecionado</option><option value="t" selected>Todo o Rio Grande do Sul ('+
      (META.dists.length>3?META.dists.length+" distribuidoras":META.dists.map(d=>esc(d.n)).join(" e "))+')</option>'+META.dists.map(d=>`<option value="t:${d.k}">Toda a ${esc(d.n)}</option>`).join("");
    distEl.innerHTML='<option value="">Todas</option>'+META.dists.map(d=>`<option value="${d.k}">${esc(d.n)}</option>`).join("");
    distEl.addEventListener("change",()=>{listarMuns();const m=META.muns.find(x=>munK(x)===META.munK);
      if(distEl.value&&(!m||m.d!==distEl.value)){const dd=META.dists.find(d=>d.k===distEl.value);
        const alvo=(dd&&dd.padrao)||(munEl.options[0]&&munEl.options[0].value);if(alvo)carregarMun(alvo)}})}
  listarMuns();
  munEl.addEventListener("change",()=>carregarMun(munEl.value));
  const b=META.bboxAll;map.fitBounds([[b[0],b[1]],[b[2],b[3]]]);
  carregarMun((h=>h?munK(h):META.default)(META.muns.find(m=>munK(m)===hMun)||
    META.muns.find(m=>m.c===hMun&&(!META.dists||m.d===META.dists[0].k))||META.muns.find(m=>m.c===hMun))).then(()=>{const t=hTr&&D.find(x=>String(x.id)===hTr);if(t)abrir(t)});
}else{desenhar();render();ajustar();const t=hTr&&D.find(x=>String(x.id)===hTr);if(t)abrir(t)}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
