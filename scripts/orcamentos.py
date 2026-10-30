#!/usr/bin/env python3
"""Orçamentos de conexão da RGE (CPFL) → dados/orc.json (estudos da distribuidora por trafo).

O orçamento de conexão de microgeração traz, para o trafo do cliente e para os trafos vizinhos,
o sentido do fluxo (direto ou inverso) e o limite de potência injetável calculados pela própria
distribuidora. O número do trafo no orçamento ("número operativo") NÃO existe na BDGD, então o
trafo é achado pela posição da UC + alimentador + kVA + nº de UCs + GD instalada.

Grava só dados do trafo e do alimentador. Nome, CPF/CNPJ, endereço, nº da UC, coordenada e nº do
pedido do cliente NÃO saem do PDF. Os PDFs nunca vão para o repositório.

Uso (precisa do pdftotext, pacote poppler-utils):
  python3 -I scripts/orcamentos.py orcamento1.pdf orcamento2.pdf ... --site .
  python3 -I scripts/orcamentos.py pasta_com_pdfs/ --site .
Junta com o dados/orc.json que já existe (um estudo repetido não é gravado duas vezes).

dados/orc.json:
  {"t": {"<id do trafo na BDGD>": {"m": "<código IBGE>", "e": [estudo, ...]}},
   "a": {"<alimentador>": [estudo de alimentador, ...]}}
  estudo de trafo: {tr: nº do trafo na distribuidora, d: data (AAAA-MM-DD), f: "D" direto | "I" inverso,
     lim: kW que ainda cabem sem inverter o fluxo, ucs, ugd: UCs com GD, gd: kW de GD, v: 1 = trafo vizinho
     do pedido, kva, cmin: carga mínima de dia estimada pela distribuidora (kW, sem descontar a GD),
     pk: kW pedidos, lib: kW liberados sem obra, por: de onde veio o lib ("pg" = cálculo de Pg pelo consumo
     do cliente, "fluxo" = folga do fluxo do trafo, "gd" = limitado à GD que o cliente já tem, trafo invertido)}
  estudo de alimentador: {d, f, min: menor fluxo de dia (kW, negativo = inversão), ucs, gd, pk, lib}
"""
import argparse, glob, json, math, os, re, subprocess, sys

MESES = "JANEIRO FEVEREIRO MARÇO ABRIL MAIO JUNHO JULHO AGOSTO SETEMBRO OUTUBRO NOVEMBRO DEZEMBRO".split()
JANELA = (9, 16)   # janela solar (h) para a carga mínima de dia


def num(s):
    s = (s or "").replace("R$", "").strip()
    if s in ("N/A", "-", ""):
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def dms(s):
    out = []
    for g, mi, se, h in re.findall(r"(\d+)°(\d+)'([\d.]+)\"([NSWO])", s):
        v = int(g) + int(mi) / 60 + float(se) / 3600
        out.append(-v if h in "SWO" else v)
    return out


def tabela_horas(txt, titulo):
    i = txt.find(titulo)
    if i < 0:
        return None
    lin = re.findall(r"(\d+):00 às (\d+):00\s+(-?[\d.,]+)\s+(-?[\d.,]+)\s+(-?[\d.,]+)", txt[i:i + 4000])
    return [[num(a), num(b), num(c)] for _, _, a, b, c in lin[:24]] if len(lin) >= 24 else None


def ler_texto(t):
    """Extrai do texto do orçamento (pdftotext -layout) os dados técnicos."""
    u = re.sub(r"\s+", " ", t)
    o = {}
    m = re.search(r"/RS, (\d+) DE (\w+) DE (\d{4})", t)
    o["data"] = m and f"{m.group(3)}-{MESES.index(m.group(2).upper()) + 1:02d}-{int(m.group(1)):02d}"
    m = re.search(r"Coordenadas geográficas da UC\s+(.+)", t)
    c = dms(m.group(1)) if m else []
    o["lat"], o["lon"] = (c + [None, None])[:2]
    m = re.search(r"2\.2 Potência:\s+([\d.,]+) kW", t)
    o["pk"] = m and num(m.group(1))
    o["nivel"] = "alimentador" if re.search(r"atendida pelo alimentador \w+", u) else "trafo"
    m = re.search(r"transformação (\d+) \(([\d.,]+)\s*kVA", u)
    o["tr"], o["kva"] = (m.group(1), num(m.group(2))) if m else (None, None)
    m = re.search(r"possui (\d+) UCs atendidas,(?: das quais \d+ UCs .*?)? totalizando ([\d.,]+) kW", u)
    o["ucs"], o["gd"] = (int(m.group(1)), num(m.group(2))) if m else (None, None)
    m = re.search(r"GD já conectada\s+(\d+)\s+([\d.,]+)", t)
    o["ugd"] = m and int(m.group(1))
    m = re.search(r"\(alimentador (\w+)\)", u) or re.search(r"atendida pelo alimentador (\w+)", u)
    o["alim"] = m and m.group(1)
    o["sem"], o["com"] = tabela_horas(t, "Tabela 3.2"), tabela_horas(t, "Tabela 3.3")
    o["viz"] = []
    i = t.find("Tabela 4.2")
    if i >= 0:
        for a, b, c, d, e, f in re.findall(r"^\s*(\d{5,8})\s+(\d+)\s+(\d+)\s+([\d.,]+)\s+(Direto|Inverso)\s+([\d.,]+)",
                                           t[i:i + 3000], re.M):
            o["viz"].append({"tr": a, "ucs": int(b), "ugd": int(c), "gd": num(d), "f": e[0], "lim": num(f)})
    # Pg (Tabela 4.1); sem 12 meses de histórico não há Pg
    o["pg"] = None
    i = t.find("Tabela 4.1")
    if i >= 0 and "não possui histórico" not in u:
        m = re.search(r"^\s*𝑃𝑔\s.*?([\d.,]+)\s*$", t[i:i + 1500], re.M)
        o["pg"] = m and num(m.group(1))
    # potência liberada sem obra: alternativa IV (redução permanente da potência injetável)
    o["lib"] = None
    i = t.find("Tabela 5.1")
    if i >= 0:
        m = re.search(r"^\s*IV\s+Sim\s+(.*)$", t[i:i + 3000], re.M)
        if m:
            p = re.split(r"\s{2,}", m.group(1).strip())
            v = re.findall(r"[\d.,]+", p[2]) if len(p) > 2 else []
            o["lib"] = num(v[-1]) if v else None
    # de onde veio a potência liberada
    o["por"] = None
    if o["lib"] is not None:
        if re.search(r"potência de geração distribuída já existente em sua instalação", u):
            o["por"] = "gd"
        elif o["pg"] is not None and abs(o["lib"] - o["pg"]) < 0.01:
            o["por"] = "pg"
        else:
            o["por"] = "fluxo"
    return o


def pdf_texto(pdf):
    try:
        return subprocess.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("Falta o pdftotext (instale o pacote poppler-utils).")


def fluxo_dia(curva):
    """Menor fluxo no trafo na janela solar (kW), nos três tipos de dia."""
    return min(min(r) for r in curva[JANELA[0]:JANELA[1]]) if curva else None


def carga_bruta(o):
    """Carga mínima de dia estimada pela distribuidora, sem descontar a GD: fluxo + geração da GD existente.
    O perfil de geração por kWp sai da diferença entre as tabelas com e sem a GD pedida."""
    s, c, pk = o["sem"], o["com"], o["pk"]
    if not (s and c and pk and o["gd"] is not None):
        return None
    fg = [max((s[h][0] - c[h][0]) / pk, 0) for h in range(24)]
    return min(min(s[h][j] + fg[h] * o["gd"] for j in range(3)) for h in range(*JANELA))


class Trafos:
    def __init__(self, pasta):
        self.t = []
        for f in glob.glob(os.path.join(pasta, "43*.json")):
            mun = os.path.basename(f)[:-5]
            for x in json.load(open(f, encoding="utf-8")):
                if x.get("lat") is not None:
                    self.t.append(dict(x, mun=mun))

    def candidatos(self, la, lo, alim=None, kva=None, ucs=None, gd=None, raio=600):
        k = 111e3 * math.cos(math.radians(la))
        out = []
        for t in self.t:
            if abs(t["lat"] - la) > raio / 111e3 or abs(t["lon"] - lo) > raio / k:
                continue
            d = math.hypot((t["lat"] - la) * 111e3, (t["lon"] - lo) * k)
            if d > raio:
                continue
            s = d / 100
            if alim and t.get("alim") != alim: s += 5
            if kva and t.get("kva") != kva: s += 3
            if ucs is not None: s += abs((t.get("ucs") or 0) - ucs) / max(ucs, 5) * 4
            if gd is not None: s += abs((t.get("gd") or 0) - gd) / max(gd, 5) * 4
            out.append((s, t))
        out.sort(key=lambda x: x[0])
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+", help="PDFs dos orçamentos (ou pastas com PDFs)")
    ap.add_argument("--site", default=".", help="raiz do site (lê e grava <site>/<pasta>/orc.json)")
    ap.add_argument("--pasta", default="dados", help="pasta de dados da distribuidora (padrão: dados = RGE)")
    ap.add_argument("--max-score", type=float, default=3.5, help="nota máxima para aceitar o casamento")
    a = ap.parse_args()

    arqs = []
    for p in a.pdfs:
        arqs += sorted(glob.glob(os.path.join(p, "*.pdf"))) if os.path.isdir(p) else [p]
    pasta = os.path.join(a.site, a.pasta)
    tr = Trafos(pasta)
    print(f"{len(tr.t):,} trafos em {pasta}; {len(arqs)} PDFs")

    cand = {}       # nº do trafo na distribuidora -> lista de (nota, margem, id, estudo)
    alims = {}
    for arq in arqs:
        o = ler_texto(pdf_texto(arq))
        nome = os.path.basename(arq)
        if o["lat"] is None or not o["data"]:
            print(f"  {nome}: não reconheci o formato, ignorado")
            continue
        if o["nivel"] == "alimentador":
            if o["alim"]:
                alims.setdefault(o["alim"], []).append({"d": o["data"], "f": "I" if (fluxo_dia(o["sem"]) or 0) < 0 else "D",
                    "min": round(fluxo_dia(o["sem"]), 1) if o["sem"] else None, "ucs": o["ucs"], "gd": o["gd"],
                    "pk": o["pk"], "lib": o["lib"], "por": o["por"]})
                print(f"  {nome}: estudo do alimentador {o['alim']}")
            continue
        fl = min(min(r) for r in o["sem"]) if o["sem"] else None
        inv = fl is not None and fl < 0
        # folga do trafo: o menor fluxo; se a liberação veio do fluxo, o valor oficial da alternativa IV
        lim = 0 if inv else (o["lib"] if o["por"] == "fluxo" else (round(fl, 2) if fl is not None else None))
        e = {"tr": o["tr"], "d": o["data"], "f": "I" if inv else "D", "lim": lim, "ucs": o["ucs"], "ugd": o["ugd"],
             "gd": o["gd"], "v": 0, "kva": o["kva"], "pk": o["pk"], "lib": o["lib"], "por": o["por"]}
        cb = carga_bruta(o)
        if cb is not None:
            e["cmin"] = round(cb, 2)
        c = tr.candidatos(o["lat"], o["lon"], o["alim"], o["kva"], o["ucs"], o["gd"])
        if c:
            cand.setdefault(o["tr"], []).append((c[0][0], (c[1][0] - c[0][0]) if len(c) > 1 else 99, c[0][1], e))
        for v in o["viz"]:
            cv = [x for x in tr.candidatos(o["lat"], o["lon"], o["alim"], None, v["ucs"], v["gd"], raio=900)
                  if not c or x[1]["id"] != c[0][1]["id"]]
            if cv:
                ev = {"tr": v["tr"], "d": o["data"], "f": v["f"], "lim": v["lim"], "ucs": v["ucs"], "ugd": v["ugd"],
                      "gd": v["gd"], "v": 1}
                cand.setdefault(v["tr"], []).append((cv[0][0], (cv[1][0] - cv[0][0]) if len(cv) > 1 else 99, cv[0][1], ev))
        print(f"  {nome}: trafo {o['tr']} + {len(o['viz'])} vizinho(s)")

    # um trafo da distribuidora -> um trafo da BDGD (o de menor nota; empate ou nota ruim fica de fora)
    escolha = []
    for n, l in cand.items():
        # o estudo do próprio pedido (com kVA) manda no casamento; vizinho só quando não há
        prop = [x for x in l if not x[3]["v"]]
        s, mg, t, _ = min(prop or l, key=lambda x: x[0])
        if s <= a.max_score and mg >= 0.3:
            escolha.append((s, n, t))
        else:
            print(f"  trafo {n}: casamento incerto (nota {s:.2f}, margem {mg:.2f}), não gravado")
    usado, ok = {}, {}
    for s, n, t in sorted(escolha, key=lambda x: x[0]):
        if t["id"] in usado:
            print(f"  trafo {n}: o trafo {t['id']} já ficou com {usado[t['id']]}, não gravado")
            continue
        usado[t["id"]] = n
        ok[n] = t

    arq_saida = os.path.join(pasta, "orc.json")
    base = json.load(open(arq_saida, encoding="utf-8")) if os.path.exists(arq_saida) else {"t": {}, "a": {}}
    novos = 0
    for n, t in ok.items():
        r = base["t"].setdefault(str(t["id"]), {"m": t["mun"], "e": []})
        for *_, e in cand[n]:
            if not any(x["tr"] == e["tr"] and x["d"] == e["d"] and x["v"] == e["v"] for x in r["e"]):
                r["e"].append({k: v for k, v in e.items() if v is not None}); novos += 1
        r["e"].sort(key=lambda x: (x["d"], -x["v"]), reverse=True)
    for al, l in alims.items():
        r = base["a"].setdefault(al, [])
        for e in l:
            if not any(x["d"] == e["d"] and x.get("pk") == e.get("pk") for x in r):
                r.append({k: v for k, v in e.items() if v is not None}); novos += 1
        r.sort(key=lambda x: x["d"], reverse=True)
    with open(arq_saida, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{arq_saida}: {len(base['t'])} trafos, {len(base['a'])} alimentadores ({novos} estudos novos)")


if __name__ == "__main__":
    main()
