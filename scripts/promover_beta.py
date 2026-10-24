#!/usr/bin/env python3
"""Promove o beta para o site oficial: copia beta/index.html para index.html.
Uso: python3 scripts/promover_beta.py   (depois: commit e push na main)"""
from pathlib import Path
raiz = Path(__file__).resolve().parent.parent
b, o = raiz / "beta" / "index.html", raiz / "index.html"
o.write_text(b.read_text(encoding="utf-8"), encoding="utf-8")
print(f"{b} → {o}")
