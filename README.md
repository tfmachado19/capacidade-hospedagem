# Capacidade de Hospedagem de GD — área da RGE

Plataforma de consulta da capacidade estimada de cada transformador de distribuição
da área atendida pela RGE (376 municípios do RS) para receber nova geração distribuída
(GD). Abre em Santa Maria; troque o município no seletor, pela busca ou pelo link
`#<código IBGE>` (ex.: `.../capacidade-hospedagem/#4305108` = Caxias do Sul).

Ao informar a "GD pretendida", o mapa mostra só os trafos cuja capacidade disponível
estimada comporta essa potência (dá para desligar o filtro).
A opção "Só trafos com 1 unidade consumidora" restringe o mapa a esses trafos.

Cada trafo mostra também o carregamento estimado do seu alimentador: demanda de pico
(energia injetada no mês de maior energia, da BDGD/CTMT, × fator de pico das curvas
típicas das cargas) ÷ capacidade do condutor do trecho de saída (√3 × tensão nominal ×
corrente nominal do SEGCON). Não considera limites de proteção nem manobras; valores
muito acima de 100% indicam alimentador com dado atípico na BDGD e devem ser conferidos.

Site: https://tfmachado19.github.io/capacidade-hospedagem

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
