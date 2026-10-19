# Capacidade de Hospedagem de GD — Santa Maria/RS

Plataforma de consulta da capacidade estimada de cada transformador de distribuição
de Santa Maria/RS (IBGE 4316907) para receber nova geração distribuída (GD).

Site: https://tfmachado19.github.io/capacidade-hospedagem

Os dados vêm da BDGD (Base de Dados Geográfica da Distribuidora) da RGE, publicada
pela ANEEL. São estimativas para triagem: não são medição e não substituem a análise
de inversão de fluxo da distribuidora.

## Como atualizar

1. Instale as bibliotecas (uma vez só):

       py -m pip install geopandas pyogrio openpyxl

2. Baixe a BDGD mais recente da RGE (um .zip com um .gdb dentro). Para achar o item:
   https://www.arcgis.com/sharing/rest/search?q=RGE%20BDGD&f=json&num=50
   — pegue o item `RGE_396_<data-base>_...` com a data-base mais recente e baixe em
   `https://www.arcgis.com/sharing/rest/content/items/<id>/data`.

3. Rode o script (pode passar o .zip direto):

       py scripts/trafos_bdgd.py "C:\caminho\RGE_396_xxxx.zip" --mun 4316907

   Ele gera `BDGD_trafos_4316907.xlsx` e `Capacidade_Hospedagem_4316907.html`.

4. Confira a aba **Resumo** da planilha. Se "POT_NOM em valores comerciais de kVA"
   ficar abaixo de 80%, o campo pode estar codificado: veja a coluna "POT_NOM (bruto)".

5. Renomeie `Capacidade_Hospedagem_4316907.html` para `index.html`, substitua o arquivo
   na raiz do repositório e faça commit/push na `main`. O GitHub Pages publica em
   alguns minutos. Não envie a planilha nem o zip.
