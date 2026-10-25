#!/usr/bin/env python3
"""Atualiza o código (visual/JS) de uma página do site a partir do TEMPLATE de trafos_bdgd.py,
mantendo os dados embutidos dela (META: municípios, data-base, título da distribuidora).

Uso:  python3 scripts/atualizar_codigo.py ceee/index.html [outra/index.html ...]
Use depois de promover o beta para o oficial, para levar as mesmas melhorias à página da CEEE."""
import importlib.util, json, re, sys
from pathlib import Path

aqui = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tb", aqui / "trafos_bdgd.py")
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

for alvo in sys.argv[1:]:
    p = Path(alvo)
    s = p.read_text(encoding="utf-8")
    meta = json.loads(re.search(r"const META=(\{.*?\});\n", s).group(1))
    p.write_text(tb._html("null", meta, meta.get("titulo") or "Área de concessão"), encoding="utf-8")
    print(f"{p}: código atualizado ({len(meta.get('muns', []))} municípios, título {meta.get('titulo')!r})")
