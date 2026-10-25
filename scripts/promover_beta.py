#!/usr/bin/env python3
"""Promove o beta para o site oficial: copia beta/index.html para index.html.
Uso: python3 scripts/promover_beta.py   (depois: commit e push na main)"""
from pathlib import Path
raiz = Path(__file__).resolve().parent.parent
b, o = raiz / "beta" / "index.html", raiz / "index.html"
o.write_text(b.read_text(encoding="utf-8"), encoding="utf-8")
print(f"{b} → {o}")
# a chave do Google testada no beta (beta/config.json) passa a valer no oficial (dados/config.json)
import json
cb, co = raiz / "beta" / "config.json", raiz / "dados" / "config.json"
if cb.exists():
    kb = json.loads(cb.read_text(encoding="utf-8")).get("googleMapsKey", "")
    if kb:
        d = json.loads(co.read_text(encoding="utf-8")) if co.exists() else {}
        d["googleMapsKey"] = kb
        co.write_text(json.dumps(d) + "\n", encoding="utf-8")
        print(f"chave do Google: {cb} → {co}")
# página única com várias distribuidoras: o endereço antigo /ceee/ passa a abrir a página única na CEEE
if '"dists":' in o.read_text(encoding="utf-8"):
    (raiz / "ceee" / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>CEEE Equatorial</title>'
        '<meta http-equiv="refresh" content="0; url=../#CEEE:4314902">'
        '<script>location.replace("../"+(location.hash||"#CEEE:4314902").replace(/^#(\\d)/,"#CEEE:$1"))</script>'
        '<a href="../#CEEE:4314902">Abrir o mapa da CEEE Equatorial</a>\n', encoding="utf-8")
    print("ceee/index.html → redireciona para a página única")
