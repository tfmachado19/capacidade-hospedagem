# Capacidade de Hospedagem de GD — área da RGE

Plataforma de consulta da capacidade estimada de cada transformador de distribuição
da área atendida pela RGE (376 municípios do RS) para receber nova geração distribuída
(GD). Abre em Santa Maria; troque o município no seletor, pela busca ou pelo link
`#<código IBGE>` (ex.: `.../capacidade-hospedagem/#4305108` = Caxias do Sul).

Ao informar a "GD pretendida", o mapa mostra só os trafos cuja capacidade disponível
estimada comporta essa potência (dá para desligar o filtro).
A opção "Só trafos com 1 unidade consumidora" restringe o mapa a esses trafos. O filtro "Trafo de … até … kVA"
limita o mapa a uma faixa de potência do transformador.

Cada trafo mostra também o carregamento estimado do seu alimentador: demanda de pico
(energia injetada no mês de maior energia, da BDGD/CTMT, × fator de pico das curvas
típicas das cargas) ÷ capacidade do condutor do trecho de saída (√3 × tensão nominal ×
corrente nominal do SEGCON). Não considera limites de proteção nem manobras; valores
muito acima de 100% indicam alimentador com dado atípico na BDGD e devem ser conferidos.

Para GD, cada alimentador mostra: GD ligada (BT + MT, pela base da ANEEL), carregamento
de GD (GD ÷ capacidade do condutor de saída) e capacidade para nova GD = menor entre
(carga mínima diurna do alimentador − GD) e (capacidade − GD). A carga mínima do
alimentador usa a energia medida na saída (já líquida da GD) somada à geração estimada
da GD. A opção "Só trafos cujo alimentador comporta a GD pretendida" mostra só os trafos
cujo alimentador tem capacidade para nova GD maior ou igual à GD informada.

Ao abrir um trafo, o mapa desenha a rede BT dele (camada SSDBT da BDGD) e marca em
amarelo os postes com GD (UGBT ligadas pelo ponto de conexão PN_CON → PONNOT), com a
potência somada por poste. A posição é a do poste, não a da unidade. Esses dados ficam em
`dados/bt/<alimentador>.json` e são baixados só quando um trafo é aberto.

Site: https://tfmachado19.github.io/capacidade-hospedagem

## Trabalho em equipe com Claude Code

1. Peça ao dono do repositório para te convidar como colaborador (GitHub → Settings →
   Collaborators).
2. Conecte seu GitHub ao Claude em https://claude.ai/connect-github e, se pedir, instale o app
   Claude no repositório.
3. Abra o Claude Code (web em https://claude.ai/code, app ou terminal) e escolha o repositório
   `tfmachado19/capacidade-hospedagem`.
4. Para baixar a BDGD e as bases da ANEEL, o ambiente de nuvem precisa liberar os domínios
   `www.arcgis.com`, `*.arcgis.com` e `dadosabertos.aneel.gov.br` (menu do ambiente → Edit →
   Network access). As bibliotecas Python são instaladas sozinhas ao abrir a sessão.
5. Peça as mudanças em português. O Claude lê o `CLAUDE.md` (regras e premissas do projeto),
   testa e publica direto na `main` (o site atualiza em 1–2 minutos). Todos os colaboradores
   podem publicar; o histórico de mudanças fica em Commits, no GitHub, e dá para desfazer.

Pedidos e problemas: aba **Issues** do GitHub (há um modelo pronto).

Os dados vêm da BDGD (Base de Dados Geográfica da Distribuidora) da RGE, publicada
pela ANEEL. São estimativas para triagem: não são medição e não substituem a análise
de inversão de fluxo da distribuidora.

## Como atualizar

1. Instale as bibliotecas (uma vez só):

       py -m pip install geopandas pyogrio openpyxl pyarrow

2. Baixe a BDGD mais recente da RGE (um .zip com um .gdb dentro). Para achar o item:
   https://www.arcgis.com/sharing/rest/search?q=RGE%20BDGD&f=json&num=50
   — pegue o item `RGE_396_<data-base>_...` com a data-base mais recente e baixe em
   `https://www.arcgis.com/sharing/rest/content/items/<id>/data`.

3. Baixe a base oficial de GD da ANEEL (`empreendimento-geracao-distribuida.parquet`):
   https://dadosabertos.aneel.gov.br/dataset/relacao-de-empreendimentos-de-geracao-distribuida

   Ela é necessária porque, na BDGD da RGE, o campo `POT_INST` das unidades geradoras
   vem como energia do melhor mês ÷ 720 h (geração média), e não como potência
   instalada. O script cruza cada UG pelo código `CEG_GD` e usa a potência oficial.

4. Rode o script para a área inteira (pode passar o .zip direto; leva uns 4 min):

       py scripts/trafos_bdgd.py "C:\caminho\RGE_396_xxxx.zip" --mun todos --mun-nome RGE --gd-aneel "C:\caminho\empreendimento-geracao-distribuida.parquet"

   Ele gera `BDGD_trafos_todos.xlsx` (abas Transformadores, Municípios, Alimentadores,
   Resumo, Leia-me) e a pasta `site_todos/` (`index.html` + `dados/<código IBGE>.json`).
   Com `--mun 4316907` gera só um município, num HTML único que abre direto do disco.

5. Confira a aba **Resumo** da planilha. Se "POT_NOM em valores comerciais de kVA"
   ficar abaixo de 80%, o campo pode estar codificado: veja a coluna "POT_NOM (bruto)".
   Confira também a linha "Fonte da potência da GD" (deve citar a base da ANEEL).

6. Copie o conteúdo de `site_todos/` (o `index.html` e a pasta `dados/`) para a raiz do
   repositório, substituindo os atuais, e faça commit/push na `main`. O GitHub Pages publica em
   alguns minutos. Não envie a planilha nem o zip.

## Prospecção comercial

`scripts/prospeccao.py` gera `Prospeccao_<município|todos>.xlsx` com empresas (endereço,
bairro, CNAE, consumo, coordenada, se já tem GD e se a rede comporta o sistema),
bairros (penetração de GD e potencial), atividades e municípios. Usa as bases UCBT_PJ e
UCMT_PJ da ANEEL (página da BDGD no portal de dados abertos), a BDGD e a planilha do
`trafos_bdgd.py`:

    py scripts/prospeccao.py --bdgd RGE_396_xxxx.zip --ucbt-pj ucbt_pj.zip --ucmt-pj ucmt_pj.csv --trafos BDGD_trafos_todos.xlsx --nomes empreendimento-geracao-distribuida.parquet

Com `--site .` (raiz do repositório) ele grava também `dados/emp/<código IBGE>.json`, usado
pela seção "Prospecção de empresas" do site: filtros por atividade, consumo mínimo, BT/MT,
GD e se a rede comporta, ficha da empresa e botão para baixar a lista filtrada em CSV.
Cada empresa traz o consumo médio dos 12 meses e o Pg para afastamento da análise de
inversão de fluxo: Pg = consumo médio ÷ (16% × 24 h × 30 dias) × FA da classe (Anexo II:
residencial 46%, industrial 69%, comercial 63%, rural 53%, serviço/poder público 57%),
arredondado para cima (REN 1.000, arts. 73-A e 655-B; Manual de Inversão de Fluxo, item 8.12).
Passar o mouse (ou tocar) num trafo ou numa empresa mostra o resumo direto no mapa.
Com a camada "Satélite" ligada, um selo no canto do mapa mostra a data, a resolução e o
fornecedor da imagem no centro da tela (metadados públicos da Esri). As fichas do trafo e da
empresa têm o botão "Satélite no Google", que abre o Google Maps na visão satélite no ponto.
Ao clicar numa empresa de BT, o site destaca o trafo dela, desenha a rede BT desse trafo e
traceja a ligação até a empresa. Numa empresa de MT, desenha o alimentador inteiro em azul
(traçado da camada SSDMT, simplificado, em `dados/mt/<alimentador>.json`).

Não envie as planilhas de prospecção ao repositório.
