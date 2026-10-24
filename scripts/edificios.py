#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edificios.py — Contornos de prédios (telhados) para a aba "Medir" do site.

Fonte: Overture Maps (tema buildings), que junta OpenStreetMap, Google Open Buildings e
Microsoft ML Buildings. Licença ODbL (atribuição "© OpenStreetMap contributors, Overture Maps").

USO
  1) Baixe os prédios da região (parquet com colunas geometry/bbox/fonte), ex.: com o script
     de exemplo no CLAUDE.md, a partir de s3://overturemaps-us-west-2/release/<versão>/theme=buildings/
  2) py scripts/edificios.py edificios_rge.parquet --site .
     → grava dados/edif/14/<x>/<y>.json (quadrados de ~2,4 km, zoom 14), só com prédios
       a até ~250 m de algum trafo da distribuidora (lidos de dados/<município>.json).

Formato de cada arquivo: {"o":[x0,y0], "b":[[fonte, x, y, dx1, dy1, ...], ...]}
  coordenadas em graus × 1e6 (≈ 0,1 m); x,y do 1º canto relativos a "o"; demais em diferenças.
  fonte: 0 OpenStreetMap, 1 Google, 2 Microsoft.
"""
import argparse, json, math, sys, time
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
import pyarrow.compute as pc
import shapely

FONTE = {"OpenStreetMap": 0, "Google Open Buildings": 1, "Microsoft ML Buildings": 2}
Z = 14
CEL = 0.0025  # célula de ~250 m para o filtro "perto de trafo"


def tile(lon, lat, z=Z):
    n = 2 ** z
    x = np.floor((lon + 180) / 360 * n).astype(np.int64)
    la = np.radians(lat)
    y = np.floor((1 - np.log(np.tan(la) + 1 / np.cos(la)) / math.pi) / 2 * n).astype(np.int64)
    return x, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("parquet")
    ap.add_argument("--site", default=".")
    ap.add_argument("--min-m2", type=float, default=12, help="ignora construções menores (m²)")
    a = ap.parse_args()
    t0 = time.time()
    site = Path(a.site)

    # células com trafo (e vizinhas)
    cel = set()
    for f in (site / "dados").glob("[0-9]*.json"):
        for t in json.loads(f.read_text(encoding="utf-8")):
            cx, cy = int(t["lon"] // CEL), int(t["lat"] // CEL)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cel.add((cx + dx, cy + dy))
    print(f"[{time.time()-t0:5.0f}s] {len(cel):,} células perto de trafos", flush=True)

    T = pq.read_table(a.parquet, columns=["geometry", "bbox", "fonte"])
    bx = T.column("bbox").combine_chunks()
    lon = (pc.struct_field(bx, "xmin").to_numpy() + pc.struct_field(bx, "xmax").to_numpy()) / 2
    lat = (pc.struct_field(bx, "ymin").to_numpy() + pc.struct_field(bx, "ymax").to_numpy()) / 2
    ok = np.fromiter(((int(x // CEL), int(y // CEL)) in cel for x, y in zip(lon, lat)), bool, len(lon))
    idx = np.flatnonzero(ok)
    print(f"[{time.time()-t0:5.0f}s] {len(idx):,} de {len(lon):,} prédios na área atendida", flush=True)

    geo = shapely.from_wkb(np.array(T.column("geometry").take(idx).to_pylist(), dtype=object))
    fonte = np.array([FONTE.get(s, 0) for s in T.column("fonte").take(idx).to_pylist()])
    lon, lat = lon[idx], lat[idx]
    # só o anel externo (telhado); multipolígonos viram o maior polígono
    geo = np.array([max(shapely.get_parts(g), key=shapely.area) if g.geom_type != "Polygon" else g for g in geo])
    m2 = shapely.area(geo) * (111320 * np.cos(np.radians(lat))) * 110540
    keep = m2 >= a.min_m2
    geo, fonte, lon, lat = geo[keep], fonte[keep], lon[keep], lat[keep]
    tx, ty = tile(lon, lat)
    print(f"[{time.time()-t0:5.0f}s] {len(geo):,} prédios ≥ {a.min_m2:g} m²", flush=True)

    out = site / "dados" / "edif" / str(Z)
    ordem = np.lexsort((ty, tx))
    chaves = np.stack([tx[ordem], ty[ordem]], 1)
    cortes = np.flatnonzero(np.any(np.diff(chaves, axis=0) != 0, axis=1)) + 1
    n = 0
    for grupo in np.split(ordem, cortes):
        x, y = int(tx[grupo[0]]), int(ty[grupo[0]])
        b = []
        for i in grupo:
            c = np.round(shapely.get_coordinates(shapely.get_exterior_ring(geo[i])) * 1e6).astype(np.int64)
            if len(c) > 1 and (c[0] == c[-1]).all():
                c = c[:-1]
            b.append([c[:, 0], c[:, 1], int(fonte[i])])
        o = [int(min(v[0].min() for v in b)), int(min(v[1].min() for v in b))]
        lin = [[f, int(cx[0] - o[0]), int(cy[0] - o[1])] + np.column_stack([np.diff(cx), np.diff(cy)]).ravel().tolist()
               for cx, cy, f in b]
        p = out / str(x)
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{y}.json").write_text(json.dumps({"o": o, "b": lin}, separators=(",", ":")), encoding="utf-8")
        n += 1
    print(f"[{time.time()-t0:5.0f}s] {n:,} arquivos em {out}", flush=True)


if __name__ == "__main__":
    main()
