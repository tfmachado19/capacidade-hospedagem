#!/usr/bin/env python3
"""Resumo das empresas de toda a área de cada distribuidora, para a aba Prospectar mostrar o estado inteiro.

Lê <pasta>dados/emp/<município>.json (gerados pelo prospeccao.py) e grava <pasta>dados/emp_estado.json:
{"a": {divisão CNAE: nome}, "e": [[lat, lon, kWh/mês, divisão, gd, rede, mt, município, índice, endereço, Pg kW], ...]}
Só empresas com consumo médio >= MIN_KWH. "índice" é a posição da empresa no arquivo do município
(a ficha completa é lida de lá ao clicar).

Uso: python3 scripts/emp_estado.py            (todas as distribuidoras do scripts/unificar.py)
"""
import json
from pathlib import Path

MIN_KWH = 1000
raiz = Path(__file__).resolve().parent.parent


def pastas():
    import importlib.util
    spec = importlib.util.spec_from_file_location("u", raiz / "scripts" / "unificar.py")
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    return [p for _, _, p, _ in u.DISTS]


def main():
    for p in pastas():
        pasta = raiz / p / "dados" / "emp"
        if not pasta.is_dir():
            continue
        nomes, linhas = {}, []
        for arq in sorted(pasta.glob("*.json")):
            d = json.loads(arq.read_text(encoding="utf-8"))
            nomes.update(d.get("a", {}))
            mun = arq.stem
            for i, x in enumerate(d["e"]):
                if x[2] >= MIN_KWH:
                    linhas.append([round(x[0], 5), round(x[1], 5), x[2], x[4], x[6], x[7], x[11], mun, i, x[8] or "", x[17]])
        linhas.sort(key=lambda x: -x[2])    # maiores primeiro (o mapa desenha as maiores de cada vista)
        destino = raiz / p / "dados" / "emp_estado.json"
        destino.write_text(json.dumps({"a": nomes, "e": linhas}, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
        print(f"{destino.relative_to(raiz)}: {len(linhas):,} empresas")


if __name__ == "__main__":
    main()
