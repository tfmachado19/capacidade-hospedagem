# Capacidade de Hospedagem de GD — RGE

Site estático (GitHub Pages, branch `main`, pasta raiz) com a capacidade estimada de cada
transformador e alimentador da RGE para receber geração distribuída (GD), mais uma camada de
prospecção comercial. Site: https://tfmachado19.github.io/capacidade-hospedagem

Responda em português do Brasil. Os usuários são engenheiros e integradores de energia solar,
nem sempre programadores: explique resultados em linguagem simples.

## Estrutura

- `index.html` — o site inteiro (HTML + CSS + JS, Leaflet). **É gerado** a partir da string
  `TEMPLATE` em `scripts/trafos_bdgd.py`.
- `beta/index.html` — versão beta do site (https://tfmachado19.github.io/capacidade-hospedagem/beta/),
  onde as mudanças são testadas antes de irem para o oficial. `scripts/promover_beta.py` copia para a raiz.
- `dados/<código IBGE>.json` — trafos de cada município (carregados ao escolher o município).
- `dados/bt/<alimentador>.json` — rede BT de cada trafo e postes com GD (ao abrir um trafo).
- `dados/mt/<alimentador>.json` — traçado da rede MT de cada alimentador (ao clicar numa empresa de MT).
- `dados/emp/<código IBGE>.json` — empresas da prospecção (ao ligar a camada de prospecção).
- `dados/invest.json` — "Usinas de investimento": trafos com 1 UC em alimentador com folga, de toda a
  área (ao abrir a aba Usinas). Gerado por `trafos_bdgd.py` junto com `dados/<código IBGE>.json`.
- `dados/edif/14/<x>/<y>.json` — contornos de prédios (aba Medir), gerados por `scripts/edificios.py`
  a partir do Overture Maps (s3://overturemaps-us-west-2/release/<versão>/theme=buildings/,
  leitura anônima com pyarrow; DuckDB não baixa extensões no ambiente de nuvem).
- `scripts/trafos_bdgd.py` — lê a BDGD e gera planilha de trafos + `index.html` + `dados/`.
- `scripts/prospeccao.py` — gera a planilha de prospecção e `dados/emp/`.

## Regras de trabalho

- Tudo que entra na `main` é publicado automaticamente pelo GitHub Pages.
- **Beta antes do oficial (obrigatório):** toda mudança no visual/JS vai primeiro para
  `beta/index.html` (e para o `TEMPLATE` em `scripts/trafos_bdgd.py`) — **não** mexa no
  `index.html` da raiz. Teste, publique na `main` e passe ao usuário o link do beta:
  https://tfmachado19.github.io/capacidade-hospedagem/beta/ . Só depois que o usuário aprovar
  ("pode publicar no oficial"), rode `python3 scripts/promover_beta.py` (copia o beta para a
  raiz), teste a raiz e publique. O mesmo arquivo funciona nos dois endereços (variável `BETA`
  no JS: etiqueta "BETA" e dados lidos de `../dados/`).
- **Dados (`dados/`) são compartilhados** entre beta e oficial: mudança de dados aparece nos
  dois. Mantenha o formato compatível com o código do oficial ou avise o usuário antes.
- Ao regenerar o site com `trafos_bdgd.py`, copie o `index.html` gerado para `beta/index.html`
  (não para a raiz) e siga o fluxo do beta.
- **Publicação:** depois de testar, envie direto para a `main`. Se o GitHub recusar o push
  (a `main` tem regra exigindo Pull Request para quem não é administrador), envie para uma
  branch e abra um Pull Request para a `main`, explicando a mudança em português simples.
- **Nunca envie ao repositório:** o zip da BDGD, as bases PJ da ANEEL, planilhas `.xlsx`
  (de trafos ou de prospecção), `__pycache__`. O `.gitignore` já cobre `.zip`, `.xlsx` e cache.
- Antes de publicar mudança no site, teste num navegador headless (Playwright/Chromium já vêm
  no ambiente de nuvem): sirva a raiz com `python3 -m http.server`, abra a página e confira que
  não há erros de JavaScript, em largura de celular (390 px) e de computador.
- Rode Python que lê dados baixados com `python3 -I`.

## Dados de entrada (fora do repositório)

| Base | Onde | Tamanho |
|---|---|---|
| BDGD da RGE (`RGE_396_<data-base>_...`) | busca: `https://www.arcgis.com/sharing/rest/search?q=RGE%20BDGD&f=json&num=50`; download: `https://www.arcgis.com/sharing/rest/content/items/<id>/data` | ~920 MB |
| Base de GD da ANEEL (`empreendimento-geracao-distribuida.parquet`) | `https://dadosabertos.aneel.gov.br/dataset/relacao-de-empreendimentos-de-geracao-distribuida` | ~110 MB |
| UCBT_PJ.zip / UCMT_PJ.csv (empresas) | página da BDGD em `dadosabertos.aneel.gov.br` (API CKAN: `package_show?id=4459e483-451f-4444-8022-bd8b5eac05c5`) | 1,3 GB / 160 MB |

O ambiente de nuvem precisa liberar `www.arcgis.com`, `*.arcgis.com` e
`dadosabertos.aneel.gov.br`. Downloads grandes: rodar em segundo plano e retomar com `curl -C -`.

## Como regenerar tudo

```bash
python3 -I scripts/trafos_bdgd.py RGE.zip --mun todos --mun-nome RGE --gd-aneel gd.parquet --saida /tmp/saida
# copie /tmp/saida/site_todos/index.html e /tmp/saida/site_todos/dados/* para a raiz do repositório
python3 -I scripts/prospeccao.py --bdgd RGE.zip --ucbt-pj ucbt_pj.zip --ucmt-pj ucmt_pj.csv \
    --trafos /tmp/saida/BDGD_trafos_todos.xlsx --nomes gd.parquet --saida /tmp/prosp --site .
```
A área inteira leva ~8 min (trafos) + ~5 min (prospecção) e usa ~5 GB de RAM.

## Premissas e armadilhas conhecidas (não "corrigir" sem discutir)

- **POT_INST da BDGD da RGE não é potência instalada**: é energia do melhor mês ÷ 720 h. A
  potência da GD vem da base da ANEEL pelo `CEG_GD` (`--gd-aneel`). Sem casamento: BT usa
  POT_INST, MT usa DEM_CONT (usinas PCH/UHE/UTE).
- **Tensão (TTEN):** código 49 = 13,8 kV, 63 = 23 kV. Outros códigos ficam sem carregamento.
- **Capacidade do trafo** = menor entre (carga mínima diurna × fator de segurança − GD) e (kVA − GD).
- **Alimentador:** capacidade = √3 × kV × corrente nominal (SEGCON.CNOM) do trecho de saída;
  carga mínima = energia medida na saída (CTMT, já líquida da GD) + geração estimada da GD
  (perfil FV do RS, ~1.150 kWh/kWp·ano) × forma das curvas típicas na janela solar.
- **Pg (afastamento da análise de inversão, REN 1.000 art. 73-A II, Manual item 8.12):**
  Pg = consumo médio 12 meses ÷ (0,16 × 24 × 30) × FA, arredondado para cima. FA: residencial
  46%, industrial 69%, comercial/serviços 63%, rural 53%, serviço/poder público 57%. Vale para
  microgeração (até 75 kW).
- **GD por UC:** o `PAC` é compartilhado por várias UCs; por isso a GD da empresa é "sim /
  provável / talvez / não".
- **pandas novo:** `astype(str)` mantém nulos como nulos. Use a função `txt()` dos scripts,
  que trata isso.
- Consumo de quem já tem GD é o líquido medido (subestima a carga).
- A API do IBGE às vezes cai; o script pega os nomes dos municípios da base de GD da ANEEL.
- `dados/config.json` guarda a chave da Map Tiles API do Google (camada "Satélite Google").
  Não apague nem sobrescreva ao copiar `site_todos/dados/`. Nunca use os endereços não oficiais
  de imagens do Google (mt*.google.com): violam os termos de uso.

## Cuidados

- O site e o repositório são **públicos**. A camada de prospecção expõe endereço e consumo de
  empresas (dados abertos da ANEEL, sem nome nem CNPJ). Não adicione dados pessoais.
- Na abordagem comercial, respeitar LGPD e o cadastro "Não Perturbe".
