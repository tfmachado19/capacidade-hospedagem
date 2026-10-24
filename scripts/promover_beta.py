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
