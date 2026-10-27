#!/usr/bin/env python3
"""Curvas típicas de carga por classe (residencial, comercial, rural...) tiradas de uma BDGD
que tem a camada CRVCRG preenchida (ex.: RGE).

Várias cooperativas e permissionárias publicam a BDGD sem curvas típicas (CRVCRG vazia e TIP_CC
em branco). Sem curva, a carga mínima de dia do trafo sai como a média do dia, o que superestima
a folga para GD. O trafos_bdgd.py usa estas curvas por classe (campo CLAS_SUB das UCs) quando a
UC não tem curva própria.

Cada curva da classe é a média das curvas das UCs da classe, ponderada pela energia anual,
normalizada para média semanal = 1 (mesmo formato do montar_curvas).

Uso: python3 -I scripts/curvas_padrao.py RGE.zip   → grava scripts/curvas_padrao.json
"""
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

raiz = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tb", raiz / "trafos_bdgd.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)


def main():
    gdb, nome = tb.abrir_gdb(sys.argv[1])
    camadas = [c[0] for c in tb.pyogrio.list_layers(gdb)]
    L = {k: tb.achar_camada(camadas, k, False) for k in tb.CAND}
    curvas = tb.montar_curvas(gdb, L["crv"])
    if not curvas:
        sys.exit("Esta BDGD não tem curvas típicas (CRVCRG vazia).")
    soma, peso = {}, {}
    for chave in ("uc", "ucmt", "pip"):
        if not L.get(chave):
            continue
        d = tb.ler(gdb, L[chave], ["TIP_CC", "CLAS_SUB"] + tb.ENE)
        d["TIP_CC"] = tb.txt(d["TIP_CC"])
        d["CL"] = "IP" if chave == "pip" else d["CLAS_SUB"].pipe(tb.txt).map(tb.classe_curva)
        d["E"] = d[tb.ENE].apply(pd.to_numeric, errors="coerce").fillna(0).sum(axis=1)
        d = d[d["TIP_CC"].isin(curvas.keys()) & (d["E"] > 0) & (d["CL"] != "")]
        por = d.groupby(["CL", "TIP_CC"])["E"].sum()
        for (cl, tip), e in por.items():
            soma[cl] = soma.get(cl, 0) + curvas[tip] * e
            peso[cl] = peso.get(cl, 0) + e
    saida = {}
    for cl, s in soma.items():
        c = s / peso[cl]
        m = (5 * c[0].mean() + c[1].mean() + c[2].mean()) / 7
        saida[cl] = np.round(c / m, 4).tolist()
    destino = raiz / "curvas_padrao.json"
    destino.write_text(json.dumps({"fonte": nome, "curvas": saida}, separators=(",", ":")), encoding="utf-8")
    print(f"{destino}: classes {sorted(saida)} (fonte {nome})")


if __name__ == "__main__":
    main()
