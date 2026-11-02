# app.py
# Controle de Trajetória de Poço - versão Streamlit
# Convertido a partir do script original (Tkinter + Plotly) para uso como
# aplicação web no Geolink (hub de soluções digitais de Geociências/E&P).

import io
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.io as pio
import lasio

st.set_page_config(page_title="Controle de Trajetória de Poço", layout="wide")

# -----------------------------------------------------------------------------
# Script JS injetado no HTML do gráfico para mostrar informação ao clicar
# (preservado do original; funciona porque embutimos o HTML via
# st.components.v1.html, que renderiza em um iframe com JS ativo)
# -----------------------------------------------------------------------------
CLICK_JS = """
(function() {
  var plotDiv = document.getElementById('grafico-trajetoria');
  if (!plotDiv) { return; }

  var infoBox = document.getElementById('info-clique-trajetoria');
  if (!infoBox) {
    infoBox = document.createElement('div');
    infoBox.id = 'info-clique-trajetoria';
    infoBox.style.position = 'fixed';
    infoBox.style.top = '12px';
    infoBox.style.right = '12px';
    infoBox.style.maxWidth = '320px';
    infoBox.style.background = 'rgba(255,255,255,0.97)';
    infoBox.style.border = '2px solid #444';
    infoBox.style.borderRadius = '8px';
    infoBox.style.padding = '10px 14px';
    infoBox.style.fontFamily = 'Arial, sans-serif';
    infoBox.style.fontSize = '13px';
    infoBox.style.lineHeight = '1.5';
    infoBox.style.zIndex = 9999;
    infoBox.style.boxShadow = '2px 2px 10px rgba(0,0,0,0.35)';
    document.body.appendChild(infoBox);
  }
  infoBox.innerHTML = '<b>Clique em um ponto do Poço Executado (linha vermelha)</b><br>para ver a distância e a orientação em relação ao planejado.';

  plotDiv.on('plotly_click', function(eventData) {
    var ponto = eventData.points[0];
    if (ponto && ponto.data && ponto.data.name === 'Poço Executado' && ponto.customdata) {
      var cd = ponto.customdata;
      infoBox.innerHTML =
        '<b>Ponto clicado - Poço Executado</b><br>' +
        'MD: ' + cd[0] + '<br>' +
        'Distância 3D ao planejado: ' + cd[1] + '<br>' +
        'Distância horizontal: ' + cd[2] + '<br>' +
        'Orientação: ' + cd[3];
    } else {
      infoBox.innerHTML = 'Clique em um ponto do <b>Poço Executado</b> (linha vermelha) para ver a distância ao planejado.';
    }
  });
})();
"""


# =============================================================================
# FUNCOES DE CALCULO (logica identica ao script original)
# =============================================================================

def calcular_dog_leg_severity(data, tipo_dados):
    if tipo_dados == "LAS":
        md_col, incl_col, azim_col = 'MD', 'INCL', 'AZIM'
    else:
        md_col, incl_col, azim_col = 'Measured', 'Incl', 'Azimuth'

    dls_values = [0.0]
    md_array = data[md_col].values
    incl_array = np.radians(data[incl_col].values)
    azim_array = np.radians(data[azim_col].values)

    for i in range(1, len(data)):
        try:
            md_atual = md_array[i]
            incl_atual = incl_array[i]
            azim_atual = azim_array[i]

            md_alvo = md_atual - 30.0
            idx_referencia = i - 1
            limite_busca = max(0, i - 50)
            for j in range(i - 1, limite_busca - 1, -1):
                if md_array[j] <= md_alvo:
                    idx_referencia = j
                    break

            if (md_atual - md_array[idx_referencia]) < 5.0:
                idx_referencia = max(0, i - 1)

            md_ref = md_array[idx_referencia]
            incl_ref = incl_array[idx_referencia]
            azim_ref = azim_array[idx_referencia]

            delta_md = md_atual - md_ref

            if delta_md > 0:
                cos_dl = (np.cos(incl_atual - incl_ref) -
                          np.sin(incl_ref) * np.sin(incl_atual) * (1 - np.cos(azim_atual - azim_ref)))
                cos_dl = np.clip(cos_dl, -1.0, 1.0)
                dog_leg_rad = np.arccos(cos_dl)
                dls = (np.degrees(dog_leg_rad) * 30.0) / delta_md
            else:
                dls = 0.0
        except Exception:
            dls = 0.0

        dls_values.append(dls)

    return dls_values


def adicionar_ponto_inicial_se_necessario(data, tipo_dados):
    if tipo_dados == "LAS":
        md_col, incl_col, azim_col, tvd_col = 'MD', 'INCL', 'AZIM', 'TVD'
    elif tipo_dados == "UTM":
        return data
    else:
        md_col, incl_col, azim_col, tvd_col = 'Measured', 'Incl', 'Azimuth', 'TVD'

    primeira_md = data.iloc[0][md_col]

    if primeira_md > 0:
        if tipo_dados == "LAS":
            linha_inicial = {'MD': 0.0, 'INCL': 0.0, 'AZIM': 0.0, 'TVD': 0.0}
        else:
            linha_inicial = {'Measured': 0.0, 'Incl': 0.0, 'Azimuth': 0.0, 'TVD': 0.0}

        for col in data.columns:
            if col not in linha_inicial:
                if col == 'Seq' or '#' in col or 'seq' in col.lower():
                    linha_inicial[col] = 0
                else:
                    linha_inicial[col] = 0.0

        df_inicial = pd.DataFrame([linha_inicial])
        data = pd.concat([df_inicial, data], ignore_index=True)

        tvd_values = [0.0]
        for i in range(1, len(data)):
            delta_md = data.iloc[i][md_col] - data.iloc[i - 1][md_col]
            incl_rad = np.radians(data.iloc[i][incl_col])
            delta_tvd = delta_md * np.cos(incl_rad)
            tvd_values.append(tvd_values[-1] + delta_tvd)

        data[tvd_col] = tvd_values

    return data


def detectar_tipo_arquivo_linhas(linhas):
    for linha in linhas:
        linha_limpa = linha.strip().upper()
        if any(p in linha_limpa for p in ['UTM', 'X', 'Y', 'Z']) and not linha_limpa.startswith(('#', '?', 'COMPASS')):
            colunas = linha_limpa.split()
            if any('X' in col for col in colunas) and any('Y' in col for col in colunas) and any('Z' in col for col in colunas):
                return "UTM"

    for linha in linhas:
        linha_limpa = linha.strip()
        if "Measured" in linha and "Incl" in linha and "Azimuth" in linha:
            return "TXT"

    return "UNKNOWN"


def processar_arquivo_utm(linhas, mesa_rotativa):
    colunas = None
    dados = []

    for linha in linhas:
        linha_limpa = linha.strip()
        if linha_limpa.startswith("?") or linha_limpa.startswith("#") or "COMPASS" in linha_limpa:
            continue
        elif colunas is None:
            linha_upper = linha_limpa.upper()
            if any('X' in palavra for palavra in linha_upper.split()) and \
               any('Y' in palavra for palavra in linha_upper.split()) and \
               any('Z' in palavra for palavra in linha_upper.split()):
                colunas = linha_limpa.split()
        elif colunas:
            valores = linha_limpa.split()
            if len(valores) == len(colunas):
                dados.append(valores)

    if not colunas or not dados:
        raise ValueError("O arquivo UTM nao contem cabecalho ou dados validos.")

    data = pd.DataFrame(dados, columns=colunas)
    data = data.apply(pd.to_numeric, errors='coerce')
    data = data.dropna()

    col_x = col_y = col_z = None
    for col in data.columns:
        col_upper = col.upper()
        if 'X' in col_upper and col_x is None:
            col_x = col
        elif 'Y' in col_upper and col_y is None:
            col_y = col
        elif 'Z' in col_upper and col_z is None:
            col_z = col

    if col_x is None or col_y is None or col_z is None:
        raise ValueError("Nao foi possivel identificar colunas X, Y, Z no arquivo UTM")

    data = data.rename(columns={col_x: 'UTM_X', col_y: 'UTM_Y', col_z: 'UTM_Z'})

    md_values = [0.0]
    for i in range(1, len(data)):
        dx = data.iloc[i]['UTM_X'] - data.iloc[i - 1]['UTM_X']
        dy = data.iloc[i]['UTM_Y'] - data.iloc[i - 1]['UTM_Y']
        dz = data.iloc[i]['UTM_Z'] - data.iloc[i - 1]['UTM_Z']
        dist_3d = np.sqrt(dx ** 2 + dy ** 2 + dz ** 2)
        md_values.append(md_values[-1] + dist_3d)

    data['Measured'] = md_values
    data['TVD'] = mesa_rotativa - data['UTM_Z']

    incl_values = [0.0]
    azim_values = [0.0]

    for i in range(1, len(data)):
        dx = data.iloc[i]['UTM_X'] - data.iloc[i - 1]['UTM_X']
        dy = data.iloc[i]['UTM_Y'] - data.iloc[i - 1]['UTM_Y']
        dz = data.iloc[i]['UTM_Z'] - data.iloc[i - 1]['UTM_Z']

        dist_horiz = np.sqrt(dx ** 2 + dy ** 2)

        if md_values[i] - md_values[i - 1] > 0:
            incl = np.degrees(np.arctan2(dist_horiz, abs(dz)))
        else:
            incl = 0.0

        if dist_horiz > 0:
            azim = np.degrees(np.arctan2(dx, dy))
            if azim < 0:
                azim += 360
        else:
            azim = azim_values[-1]

        incl_values.append(incl)
        azim_values.append(azim)

    data['Incl'] = incl_values
    data['Azimuth'] = azim_values

    return data, "UTM"


def processar_arquivo_tradicional(linhas):
    colunas = None
    dados = []
    for linha in linhas:
        if linha.strip().startswith("?") or linha.strip().startswith("#") or "COMPASS" in linha:
            continue
        elif colunas is None and "Seq" in linha and "Measured" in linha:
            colunas = linha.strip().split()
        elif colunas:
            valores = linha.strip().split()
            if len(valores) == len(colunas):
                dados.append(valores)

    if not colunas or not dados:
        raise ValueError("O arquivo nao contem cabecalho ou dados validos.")

    data = pd.DataFrame(dados, columns=colunas)
    data = data.apply(pd.to_numeric, errors='coerce')
    data = data.dropna()

    if 'TVD' not in data.columns:
        tvd_values = [0.0]
        for i in range(1, len(data)):
            delta_md = data.iloc[i]['Measured'] - data.iloc[i - 1]['Measured']
            incl_rad = np.radians(data.iloc[i]['Incl'])
            delta_tvd = delta_md * np.cos(incl_rad)
            tvd_values.append(tvd_values[-1] + delta_tvd)
        data['TVD'] = tvd_values

    data = adicionar_ponto_inicial_se_necessario(data, "TXT")
    return data, "TXT"


def processar_manual_tradicional(linhas, linha_inicio, col_md, col_incl, col_azim):
    if min(col_md, col_incl, col_azim) < 1:
        raise ValueError("As colunas devem ser maiores que 0")

    dados = []
    for i in range(linha_inicio - 1, len(linhas)):
        linha = linhas[i].strip()
        if linha:
            valores = linha.split()
            if len(valores) >= max(col_md, col_incl, col_azim):
                try:
                    md_val = float(valores[col_md - 1])
                    incl_val = float(valores[col_incl - 1])
                    azim_val = float(valores[col_azim - 1])
                    dados.append([md_val, incl_val, azim_val])
                except ValueError:
                    continue

    if not dados:
        raise ValueError("Nao foi possivel extrair dados validos com as configuracoes fornecidas.")

    data = pd.DataFrame(dados, columns=['Measured', 'Incl', 'Azimuth'])

    tvd_values = [0.0]
    for i in range(1, len(data)):
        delta_md = data.iloc[i]['Measured'] - data.iloc[i - 1]['Measured']
        incl_rad = np.radians(data.iloc[i]['Incl'])
        delta_tvd = delta_md * np.cos(incl_rad)
        tvd_values.append(tvd_values[-1] + delta_tvd)

    data['TVD'] = tvd_values
    data = adicionar_ponto_inicial_se_necessario(data, "TXT")
    return data, "TXT"


def processar_manual_utm(linhas, linha_inicio, col_x, col_y, col_z, mesa_rotativa):
    if min(col_x, col_y, col_z) < 1:
        raise ValueError("As colunas devem ser maiores que 0")

    dados = []
    for i in range(linha_inicio - 1, len(linhas)):
        linha = linhas[i].strip()
        if linha:
            valores = linha.split()
            if len(valores) >= max(col_x, col_y, col_z):
                try:
                    x_val = float(valores[col_x - 1])
                    y_val = float(valores[col_y - 1])
                    z_val = float(valores[col_z - 1])
                    dados.append([x_val, y_val, z_val])
                except ValueError:
                    continue

    if not dados:
        raise ValueError("Nao foi possivel extrair dados validos com as configuracoes fornecidas.")

    data = pd.DataFrame(dados, columns=['UTM_X', 'UTM_Y', 'UTM_Z'])

    md_values = [0.0]
    for i in range(1, len(data)):
        dx = data.iloc[i]['UTM_X'] - data.iloc[i - 1]['UTM_X']
        dy = data.iloc[i]['UTM_Y'] - data.iloc[i - 1]['UTM_Y']
        dz = data.iloc[i]['UTM_Z'] - data.iloc[i - 1]['UTM_Z']
        dist_3d = np.sqrt(dx ** 2 + dy ** 2 + dz ** 2)
        md_values.append(md_values[-1] + dist_3d)

    data['Measured'] = md_values
    data['TVD'] = mesa_rotativa - data['UTM_Z']

    incl_values = [0.0]
    azim_values = [0.0]
    for i in range(1, len(data)):
        dx = data.iloc[i]['UTM_X'] - data.iloc[i - 1]['UTM_X']
        dy = data.iloc[i]['UTM_Y'] - data.iloc[i - 1]['UTM_Y']
        dz = data.iloc[i]['UTM_Z'] - data.iloc[i - 1]['UTM_Z']

        dist_horiz = np.sqrt(dx ** 2 + dy ** 2)

        if md_values[i] - md_values[i - 1] > 0:
            incl = np.degrees(np.arctan2(dist_horiz, abs(dz)))
        else:
            incl = 0.0

        if dist_horiz > 0:
            azim = np.degrees(np.arctan2(dx, dy))
            if azim < 0:
                azim += 360
        else:
            azim = azim_values[-1]

        incl_values.append(incl)
        azim_values.append(azim)

    data['Incl'] = incl_values
    data['Azimuth'] = azim_values
    return data, "UTM"


def carregar_arquivo(uploaded_file):
    """Le um arquivo enviado via st.file_uploader (LAS ou texto tradicional/UTM)."""
    nome = uploaded_file.name.lower()
    conteudo = uploaded_file.read()

    if nome.endswith(".las"):
        texto = conteudo.decode("utf-8", errors="ignore")
        las = lasio.read(io.StringIO(texto))
        required_curves = ['MD', 'INCL', 'AZIM', 'TVD']
        curves = {c: las[c] for c in required_curves if c in [cv.mnemonic for cv in las.curves]}
        if not curves:
            raise ValueError("Nenhuma curva necessaria (MD, INCL, AZIM, TVD) encontrada no arquivo LAS.")
        data = pd.DataFrame(curves)
        data = adicionar_ponto_inicial_se_necessario(data, "LAS")
        return data, "LAS"

    texto = conteudo.decode("utf-8", errors="ignore")
    linhas = texto.splitlines(keepends=True)
    tipo_arquivo = detectar_tipo_arquivo_linhas(linhas)

    if any(',' in linha for linha in linhas):
        linhas = [linha.replace(',', '.') for linha in linhas]

    if tipo_arquivo == "UTM":
        return processar_arquivo_utm(linhas, st.session_state.mesa_rotativa)
    else:
        return processar_arquivo_tradicional(linhas)


def calcular_coordenadas(data, tipo, mesa_rotativa, coord_origem_x, coord_origem_y):
    if tipo == "UTM":
        measured = data['Measured'].tolist()
        x = data['UTM_X'].tolist()
        y = data['UTM_Y'].tolist()
        z = data['UTM_Z'].tolist()
        return measured, x, y, z

    elif tipo == "LAS" and all(col in data.columns for col in ['MD', 'INCL', 'AZIM', 'TVD']):
        measured = data['MD'].tolist()
        incl = np.radians(data['INCL'].tolist())
        azimuth = np.radians(data['AZIM'].tolist())

        x, y, z = [coord_origem_x], [coord_origem_y], [-(measured[0] - mesa_rotativa)]
        for i in range(1, len(measured)):
            delta_md = measured[i] - measured[i - 1]
            delta_x = delta_md * np.sin(incl[i]) * np.cos(azimuth[i])
            delta_y = delta_md * np.sin(incl[i]) * np.sin(azimuth[i])
            delta_z = delta_md * np.cos(incl[i])

            x.append(x[-1] + delta_x)
            y.append(y[-1] - delta_y)
            z.append(z[-1] - delta_z)
        return measured, x, y, z

    elif tipo in ["TXT", "UTM"] and all(col in data.columns for col in ['Measured', 'Incl', 'Azimuth', 'TVD']):
        measured = data['Measured'].tolist()
        incl = np.radians(data['Incl'].tolist())
        azimuth = np.radians(data['Azimuth'].tolist())
        tvd = data['TVD'].tolist()

        x, y = [coord_origem_x], [coord_origem_y]
        for i in range(1, len(measured)):
            delta_md = measured[i] - measured[i - 1]
            delta_x = delta_md * np.sin(incl[i]) * np.cos(azimuth[i])
            delta_y = delta_md * np.sin(incl[i]) * np.sin(azimuth[i])
            x.append(x[-1] + delta_x)
            y.append(y[-1] - delta_y)

        z = [-(v - mesa_rotativa) for v in tvd]
        return measured, x, y, z
    else:
        raise ValueError("As colunas necessarias para calcular as coordenadas nao estao presentes.")


def calcular_direcao(delta_x, delta_y):
    angulo = np.degrees(np.arctan2(delta_y, delta_x))
    direcoes = ['Norte', 'Nordeste', 'Leste', 'Sudeste', 'Sul', 'Sudoeste', 'Oeste', 'Noroeste']
    idx_direcao = round(angulo / 45) % 8
    return direcoes[idx_direcao]


def formatar_orientacao(delta_x, delta_y):
    partes = []
    if abs(delta_y) >= 0.01:
        partes.append(f"{abs(delta_y):.1f} m {'Leste' if delta_y >= 0 else 'Oeste'}")
    if abs(delta_x) >= 0.01:
        partes.append(f"{abs(delta_x):.1f} m {'Norte' if delta_x >= 0 else 'Sul'}")
    if not partes:
        return "Sem desvio horizontal relevante"
    return "; ".join(partes)


def densificar_md(mds_survey, intervalo):
    mds_survey = sorted(set(float(m) for m in mds_survey))
    if intervalo is None or intervalo <= 0:
        return mds_survey

    novos_mds = []
    for i in range(len(mds_survey) - 1):
        md_ini = mds_survey[i]
        md_fim = mds_survey[i + 1]
        novos_mds.append(md_ini)
        distancia_trecho = md_fim - md_ini
        if distancia_trecho > intervalo:
            n_subpontos = int(distancia_trecho // intervalo)
            for k in range(1, n_subpontos + 1):
                md_intermediario = md_ini + k * intervalo
                if md_intermediario < md_fim:
                    novos_mds.append(md_intermediario)
    novos_mds.append(mds_survey[-1])
    return sorted(set(novos_mds))


def calcular_coordenadas_densificado(data, tipo, intervalo, mesa_rotativa, coord_x, coord_y):
    mds, xs, ys, zs = calcular_coordenadas(data, tipo, mesa_rotativa, coord_x, coord_y)
    mds_dens = densificar_md(mds, intervalo)

    mds_arr = np.array(mds, dtype=float)
    xs_dens = np.interp(mds_dens, mds_arr, xs).tolist()
    ys_dens = np.interp(mds_dens, mds_arr, ys).tolist()
    zs_dens = np.interp(mds_dens, mds_arr, zs).tolist()
    return mds_dens, xs_dens, ys_dens, zs_dens


def calcular_distancia_orientacao_pontos(md_exec_list, x_exec_list, y_exec_list, z_exec_list,
                                           md_plan_list, x_plan_list, y_plan_list, z_plan_list):
    md_plan_arr = np.array(md_plan_list, dtype=float)
    x_plan_arr = np.array(x_plan_list, dtype=float)
    y_plan_arr = np.array(y_plan_list, dtype=float)
    z_plan_arr = np.array(z_plan_list, dtype=float)

    md_min_plan = md_plan_arr.min()
    md_max_plan = md_plan_arr.max()

    resultados = []
    for md_e, x_e, y_e, z_e in zip(md_exec_list, x_exec_list, y_exec_list, z_exec_list):
        if md_e < md_min_plan or md_e > md_max_plan:
            resultados.append({
                "md": md_e, "dentro_do_planejado": False,
                "distancia_3d": None, "distancia_horizontal": None,
                "direcao_cardinal": None,
                "orientacao_detalhada": "Fora do intervalo do poco planejado"
            })
            continue

        x_p = float(np.interp(md_e, md_plan_arr, x_plan_arr))
        y_p = float(np.interp(md_e, md_plan_arr, y_plan_arr))
        z_p = float(np.interp(md_e, md_plan_arr, z_plan_arr))

        delta_x = x_e - x_p
        delta_y = y_e - y_p
        delta_z = z_e - z_p

        dist_3d = float(np.sqrt(delta_x ** 2 + delta_y ** 2 + delta_z ** 2))
        dist_horiz = float(np.sqrt(delta_x ** 2 + delta_y ** 2))

        resultados.append({
            "md": md_e, "dentro_do_planejado": True,
            "distancia_3d": dist_3d, "distancia_horizontal": dist_horiz,
            "direcao_cardinal": calcular_direcao(delta_x, delta_y),
            "orientacao_detalhada": formatar_orientacao(delta_x, delta_y)
        })
    return resultados


def gerar_elipse(x_center, y_center, raio_h, raio_v, z_level, n_pontos=100):
    t = np.linspace(0, 2 * np.pi, n_pontos)
    x = x_center + raio_h * np.cos(t)
    y = y_center + raio_v * np.sin(t)
    z = np.full_like(x, z_level)
    return x, y, z


def gerar_relatorio_csv(distancias, direcoes, dls_planejado, dls_executado, md_planejado, md_executado):
    """Monta o relatorio como texto CSV (em memoria), para download via Streamlit."""
    buf = io.StringIO()
    buf.write("RELATORIO DE CONTROLE DE TRAJETORIA COM DLS\n")
    buf.write("=" * 50 + "\n\n")

    if distancias and direcoes:
        df_dados = pd.DataFrame({"Distancia (m)": distancias, "Direcao": direcoes})
        buf.write("DADOS DE DISTANCIA E DIRECAO:\n")
    else:
        df_dados = pd.DataFrame({"Observacao": ["Dados de comparacao nao disponiveis - apenas um poco carregado"]})
        buf.write("OBSERVACAO:\n")
    df_dados.to_csv(buf, index=False)

    if dls_planejado and md_planejado:
        buf.write("\n\nDLS - POCO PLANEJADO:\n")
        pd.DataFrame({"MD (m)": md_planejado, "DLS Planejado (graus/30m)": dls_planejado}).to_csv(buf, index=False)

    if dls_executado and md_executado:
        buf.write("\n\nDLS - POCO EXECUTADO:\n")
        pd.DataFrame({"MD (m)": md_executado, "DLS Executado (graus/30m)": dls_executado}).to_csv(buf, index=False)

    if distancias:
        buf.write("\n\nRESUMO ESTATISTICO - DISTANCIAS:\n")
        pd.DataFrame({
            "Parametro": ["Distancia Maxima (m)", "Distancia Minima (m)", "Distancia Media (m)", "Desvio Padrao (m)"],
            "Valor": [max(distancias), min(distancias), np.mean(distancias), np.std(distancias)]
        }).to_csv(buf, index=False)

    if dls_planejado:
        buf.write("\n\nRESUMO ESTATISTICO - DLS PLANEJADO:\n")
        pd.DataFrame({
            "Parametro": ["DLS Maximo (graus/30m)", "DLS Minimo (graus/30m)", "DLS Medio (graus/30m)", "Desvio Padrao DLS (graus/30m)"],
            "Valor": [max(dls_planejado), min(dls_planejado), np.mean(dls_planejado), np.std(dls_planejado)]
        }).to_csv(buf, index=False)

    if dls_executado:
        buf.write("\n\nRESUMO ESTATISTICO - DLS EXECUTADO:\n")
        pd.DataFrame({
            "Parametro": ["DLS Maximo (graus/30m)", "DLS Minimo (graus/30m)", "DLS Medio (graus/30m)", "Desvio Padrao DLS (graus/30m)"],
            "Valor": [max(dls_executado), min(dls_executado), np.mean(dls_executado), np.std(dls_executado)]
        }).to_csv(buf, index=False)

    if direcoes:
        buf.write("\n\nRESUMO - DIRECOES:\n")
        moda = pd.Series(direcoes).mode()
        pd.DataFrame({"Parametro": ["Direcao Predominante"], "Valor": [moda[0] if len(moda) > 0 else "N/A"]}).to_csv(buf, index=False)

    return buf.getvalue()


def projetar_profundidade_final(data_executado, tipo_executado, md_final, tipo_projecao,
                                  nova_incl, novo_azim, mesa_rotativa, coord_x, coord_y):
    if data_executado is None:
        raise ValueError("Carregue primeiro o poco executado para fazer a projecao!")

    ultima_estacao = data_executado.iloc[-1]
    if tipo_executado == "LAS":
        md_col, incl_col, azim_col, tvd_col = 'MD', 'INCL', 'AZIM', 'TVD'
    else:
        md_col, incl_col, azim_col, tvd_col = 'Measured', 'Incl', 'Azimuth', 'TVD'

    md_atual = ultima_estacao[md_col]
    incl_atual = ultima_estacao[incl_col]
    azim_atual = ultima_estacao[azim_col]
    tvd_atual = ultima_estacao[tvd_col]

    if md_final <= md_atual:
        raise ValueError(f"A profundidade final ({md_final}m) deve ser maior que a MD atual ({md_atual:.1f}m)")

    md_list, x_list, y_list, z_list = calcular_coordenadas(data_executado, tipo_executado, mesa_rotativa, coord_x, coord_y)
    x_atual = x_list[-1]
    y_atual = y_list[-1]

    if tipo_projecao == "mesmos":
        incl_projecao = incl_atual
        azim_projecao = azim_atual
    else:
        incl_projecao = nova_incl
        azim_projecao = novo_azim

    incremento_md = 10.0
    md_projetado, x_projetado, y_projetado, tvd_projetado = [], [], [], []
    incl_projetado, azim_projetado = [], []
    md_temp, x_temp, y_temp, tvd_temp = md_atual, x_atual, y_atual, tvd_atual

    while md_temp < md_final:
        md_prox = min(md_temp + incremento_md, md_final)
        delta_md = md_prox - md_temp

        incl_rad = np.radians(incl_projecao)
        azim_rad = np.radians(azim_projecao)

        delta_tvd = delta_md * np.cos(incl_rad)
        delta_x = delta_md * np.sin(incl_rad) * np.cos(azim_rad)
        delta_y = delta_md * np.sin(incl_rad) * np.sin(azim_rad)

        md_temp = md_prox
        x_temp += delta_x
        y_temp += delta_y
        tvd_temp += delta_tvd

        md_projetado.append(md_temp)
        x_projetado.append(x_temp)
        y_projetado.append(y_temp)
        tvd_projetado.append(tvd_temp)
        incl_projetado.append(incl_projecao)
        azim_projetado.append(azim_projecao)

    if tipo_executado == "LAS":
        data_projecao = pd.DataFrame({'MD': md_projetado, 'INCL': incl_projetado, 'AZIM': azim_projetado, 'TVD': tvd_projetado})
    else:
        data_projecao = pd.DataFrame({'Measured': md_projetado, 'Incl': incl_projetado, 'Azimuth': azim_projetado, 'TVD': tvd_projetado})

    return data_projecao


def gerar_trajetoria(data_planejado, tipo_planejado, data_executado, tipo_executado,
                      raio_tol_vert, raio_tol_horiz, tipo_prof, profundidade_elipse,
                      data_sidetrack, tipo_sidetrack, intervalo_distancia,
                      data_projecao, mesa_rotativa, coord_origem_x, coord_origem_y):
    """Monta a figura Plotly 3D e o relatorio. Retorna (fig, relatorio_csv, avisos)."""
    avisos = []
    tem_planejado = data_planejado is not None and tipo_planejado is not None
    tem_executado = data_executado is not None and tipo_executado is not None

    md1 = x1 = y1 = z1 = None
    md2 = x2 = y2 = z2 = None
    md3 = x3 = y3 = z3 = None
    md4 = x4 = y4 = z4 = None

    if tem_planejado:
        md1, x1, y1, z1 = calcular_coordenadas(data_planejado, tipo_planejado, mesa_rotativa, coord_origem_x, coord_origem_y)
    if tem_executado:
        md2, x2, y2, z2 = calcular_coordenadas(data_executado, tipo_executado, mesa_rotativa, coord_origem_x, coord_origem_y)
    if data_sidetrack is not None and tipo_sidetrack is not None:
        md3, x3, y3, z3 = calcular_coordenadas(data_sidetrack, tipo_sidetrack, mesa_rotativa, coord_origem_x, coord_origem_y)
    if data_projecao is not None and tem_executado:
        md4, x4, y4, z4 = calcular_coordenadas(data_projecao, tipo_executado, mesa_rotativa, coord_origem_x, coord_origem_y)
        desloc_x = x2[-1] - x4[0]
        desloc_y = y2[-1] - y4[0]
        desloc_z = z2[-1] - z4[0]
        x4 = [xi + desloc_x for xi in x4]
        y4 = [yi + desloc_y for yi in y4]
        z4 = [zi + desloc_z for zi in z4]

    dls_planejado, dls_executado = [], []
    if tem_planejado:
        dls_planejado = calcular_dog_leg_severity(data_planejado, tipo_planejado)
    if tem_executado:
        dls_executado = calcular_dog_leg_severity(data_executado, tipo_executado)

    data_referencia = data_planejado if tem_planejado else data_executado
    x_referencia = x1 if tem_planejado else x2
    y_referencia = y1 if tem_planejado else y2

    profundidades_lista = None
    profundidade_alvo = None
    z_elipse = None

    if tipo_prof == "TVD" and 'TVD' in data_referencia.columns:
        profundidades_lista = [-v for v in data_referencia['TVD']]
        profundidade_alvo = -profundidade_elipse
        z_elipse = profundidade_alvo
    elif tipo_prof == "Cota" and 'TVD' in data_referencia.columns:
        profundidades_lista = [-(v - mesa_rotativa) for v in data_referencia['TVD']]
        profundidade_alvo = -(profundidade_elipse - mesa_rotativa)
        z_elipse = profundidade_alvo

    fig = go.Figure()

    if tem_planejado:
        fig.add_trace(go.Scatter3d(
            x=x1, y=y1, z=z1, mode='lines+markers',
            line=dict(color='blue', width=2), marker=dict(size=4, color='blue'),
            name='Poco Planejado'
        ))

    distancias, direcoes = [], []

    if tem_executado:
        intervalo_calculo = intervalo_distancia if (intervalo_distancia and intervalo_distancia > 0) else 5.0
        md2_dens, x2_dens, y2_dens, z2_dens = calcular_coordenadas_densificado(
            data_executado, tipo_executado, intervalo_calculo, mesa_rotativa, coord_origem_x, coord_origem_y
        )
        dls2_dens = np.interp(md2_dens, md2, dls_executado).tolist() if dls_executado else [0.0] * len(md2_dens)

        hover_text, customdata_exec = [], []

        if tem_planejado:
            resultados_pontos = calcular_distancia_orientacao_pontos(md2_dens, x2_dens, y2_dens, z2_dens, md1, x1, y1, z1)
            for ponto, dls_atual in zip(resultados_pontos, dls2_dens):
                md_e = ponto["md"]
                if ponto["dentro_do_planejado"]:
                    dist3d, disthoriz, orient = ponto["distancia_3d"], ponto["distancia_horizontal"], ponto["orientacao_detalhada"]
                    hover_text.append(f"MD: {md_e:.1f} m<br>Distancia 3D: {dist3d:.2f} m<br>Distancia horizontal: {disthoriz:.2f} m<br>Orientacao: {orient}<br>DLS: {dls_atual:.2f} graus/30m")
                    customdata_exec.append([f"{md_e:.1f} m", f"{dist3d:.2f} m", f"{disthoriz:.2f} m", orient])
                    distancias.append(dist3d)
                    direcoes.append(ponto["direcao_cardinal"])
                else:
                    hover_text.append(f"MD: {md_e:.1f} m<br>{ponto['orientacao_detalhada']}<br>DLS: {dls_atual:.2f} graus/30m")
                    customdata_exec.append([f"{md_e:.1f} m", "N/D", "N/D", ponto["orientacao_detalhada"]])
        else:
            for md_e, dls_atual in zip(md2_dens, dls2_dens):
                hover_text.append(f"MD: {md_e:.1f} m<br>DLS: {dls_atual:.2f} graus/30m")
                customdata_exec.append([f"{md_e:.1f} m", "N/D (sem poco planejado)", "N/D", "N/D"])

        fig.add_trace(go.Scatter3d(
            x=x2_dens, y=y2_dens, z=z2_dens, mode='lines+markers',
            line=dict(color='red', width=2), marker=dict(size=3, color='red'),
            name='Poco Executado', hovertext=hover_text, hoverinfo="text", customdata=customdata_exec
        ))

    if md3 is not None:
        fig.add_trace(go.Scatter3d(
            x=x3, y=y3, z=z3, mode='lines+markers',
            line=dict(color='green', width=2, dash='dash'), marker=dict(size=4, color='green'),
            name='Poco Sidetrack'
        ))

    if md4 is not None:
        fig.add_trace(go.Scatter3d(
            x=x4, y=y4, z=z4, mode='lines+markers',
            line=dict(color='yellow', width=3, dash='dot'), marker=dict(size=6, color='yellow', symbol='diamond'),
            name='Projecao Final'
        ))

    if raio_tol_vert > 0 and raio_tol_horiz > 0 and profundidades_lista is not None and profundidade_alvo is not None:
        def interp_xy(profundidades, x_coords, y_coords, profundidade_alvo):
            for i in range(len(profundidades) - 1):
                p0, p1 = profundidades[i], profundidades[i + 1]
                if (p0 <= profundidade_alvo <= p1) or (p1 <= profundidade_alvo <= p0):
                    if p1 - p0 != 0:
                        frac = (profundidade_alvo - p0) / (p1 - p0)
                        return (x_coords[i] + frac * (x_coords[i + 1] - x_coords[i]),
                                y_coords[i] + frac * (y_coords[i + 1] - y_coords[i]))
            diferencas = [abs(p - profundidade_alvo) for p in profundidades]
            idx = diferencas.index(min(diferencas))
            return x_coords[idx], y_coords[idx]

        x_elipse_center, y_elipse_center = interp_xy(profundidades_lista, x_referencia, y_referencia, profundidade_alvo)
        if x_elipse_center is not None and y_elipse_center is not None:
            x_elipse, y_elipse, z_elipse_arr = gerar_elipse(x_elipse_center, y_elipse_center, raio_tol_horiz, raio_tol_vert, z_elipse)
            fig.add_trace(go.Scatter3d(
                x=x_elipse, y=y_elipse, z=z_elipse_arr, mode='lines',
                line=dict(color='orange', width=4), name='Tolerancia (Elipse)', opacity=0.7
            ))
    elif raio_tol_vert > 0 or raio_tol_horiz > 0:
        avisos.append("Elipse de tolerancia nao desenhada: verifique se ambos os raios sao > 0 e se a coluna TVD esta disponivel no poco de referencia.")

    titulo = "Trajetoria 3D dos Pocos"
    if tem_planejado and tem_executado:
        titulo = "Trajetoria 3D - Comparacao Planejado vs Executado"
    elif tem_planejado:
        titulo = "Trajetoria 3D - Poco Planejado"
    elif tem_executado:
        titulo = "Trajetoria 3D - Poco Executado"

    fig.update_layout(
        title=titulo,
        scene=dict(
            xaxis=dict(title='X (m)'), yaxis=dict(title='Y (m)'),
            zaxis=dict(title='Profundidade (m)', autorange=True),
            camera=dict(eye=dict(x=0, y=2.5, z=1.5)), aspectratio=dict(x=1, y=1, z=2)
        ),
        margin=dict(l=0, r=0, t=30, b=0), height=850
    )

    relatorio_csv = gerar_relatorio_csv(distancias, direcoes, dls_planejado, dls_executado,
                                          md1 if md1 else [], md2 if md2 else [])

    return fig, relatorio_csv, avisos


# =============================================================================
# INTERFACE STREAMLIT
# =============================================================================

if "mesa_rotativa" not in st.session_state:
    st.session_state.mesa_rotativa = 25.0
if "coord_origem_x" not in st.session_state:
    st.session_state.coord_origem_x = 390695.0
if "coord_origem_y" not in st.session_state:
    st.session_state.coord_origem_y = 7525718.0
for k in ["data_planejado", "tipo_planejado", "data_executado", "tipo_executado",
          "data_sidetrack", "tipo_sidetrack", "data_projecao", "fig", "relatorio_csv"]:
    if k not in st.session_state:
        st.session_state[k] = None

st.title("Controle de Trajetoria de Poco")
st.caption("Comparacao entre poco planejado, executado e sidetrack, com calculo de DLS, "
           "projecao de profundidade final e elipse de tolerancia.")

with st.sidebar:
    st.header("Parametros Globais")
    st.session_state.mesa_rotativa = st.number_input("Mesa Rotativa (m)", value=float(st.session_state.mesa_rotativa))
    st.session_state.coord_origem_x = st.number_input("Coordenada X de origem", value=float(st.session_state.coord_origem_x), format="%.2f")
    st.session_state.coord_origem_y = st.number_input("Coordenada Y de origem", value=float(st.session_state.coord_origem_y), format="%.2f")
    st.caption("Usados apenas quando o arquivo nao traz coordenadas absolutas (arquivos tradicionais MD/Incl/Azim).")


def bloco_upload(titulo, key_prefix):
    st.subheader(titulo)
    arquivo = st.file_uploader(f"Arquivo ({titulo})", type=["las", "txt", "dat", "asc"], key=f"upload_{key_prefix}")

    if arquivo is not None:
        try:
            data_result, tipo_result = carregar_arquivo(arquivo)
            st.session_state[f"data_{key_prefix}"] = data_result
            st.session_state[f"tipo_{key_prefix}"] = tipo_result
            st.success(f"Carregado automaticamente: {len(data_result)} pontos, tipo {tipo_result}")
        except Exception as e:
            st.warning(f"Nao foi possivel carregar automaticamente ({e}). Use a configuracao manual abaixo.")
            with st.expander("Configurar carregamento manual"):
                arquivo.seek(0)
                texto = arquivo.read().decode("utf-8", errors="ignore")
                linhas_preview = texto.splitlines()
                st.text("\n".join(f"{i + 1}: {l}" for i, l in enumerate(linhas_preview[:25])))

                tipo_manual = st.radio("Tipo de arquivo", ["Tradicional (MD, Incl, Azim)", "UTM (X, Y, Z)"], key=f"tipo_manual_{key_prefix}")
                linha_inicio = st.number_input("Linha onde comecam os dados", min_value=1, value=1, step=1, key=f"linha_{key_prefix}")

                if tipo_manual.startswith("Tradicional"):
                    col_md = st.number_input("Coluna MD", min_value=1, value=1, step=1, key=f"colmd_{key_prefix}")
                    col_incl = st.number_input("Coluna INCL", min_value=1, value=2, step=1, key=f"colincl_{key_prefix}")
                    col_azim = st.number_input("Coluna AZIM", min_value=1, value=3, step=1, key=f"colazim_{key_prefix}")
                else:
                    col_x = st.number_input("Coluna X", min_value=1, value=1, step=1, key=f"colx_{key_prefix}")
                    col_y = st.number_input("Coluna Y", min_value=1, value=2, step=1, key=f"coly_{key_prefix}")
                    col_z = st.number_input("Coluna Z", min_value=1, value=3, step=1, key=f"colz_{key_prefix}")

                if st.button("Processar manualmente", key=f"btn_manual_{key_prefix}"):
                    linhas = texto.splitlines(keepends=True)
                    if any(',' in l for l in linhas):
                        linhas = [l.replace(',', '.') for l in linhas]
                    try:
                        if tipo_manual.startswith("Tradicional"):
                            d, t = processar_manual_tradicional(linhas, int(linha_inicio), int(col_md), int(col_incl), int(col_azim))
                        else:
                            d, t = processar_manual_utm(linhas, int(linha_inicio), int(col_x), int(col_y), int(col_z), st.session_state.mesa_rotativa)
                        st.session_state[f"data_{key_prefix}"] = d
                        st.session_state[f"tipo_{key_prefix}"] = t
                        st.success(f"Processado manualmente: {len(d)} pontos")
                    except Exception as e2:
                        st.error(f"Erro no processamento manual: {e2}")


col_up1, col_up2, col_up3 = st.columns(3)
with col_up1:
    bloco_upload("Poco Planejado", "planejado")
with col_up2:
    bloco_upload("Poco Executado", "executado")
with col_up3:
    bloco_upload("Poco Sidetrack (opcional)", "sidetrack")

st.markdown("---")
c1, c2, c3 = st.columns(3)
c1.metric("Planejado", "Carregado" if st.session_state.data_planejado is not None else "Nao carregado")
c2.metric("Executado", "Carregado" if st.session_state.data_executado is not None else "Nao carregado")
c3.metric("Sidetrack", "Carregado" if st.session_state.data_sidetrack is not None else "Nao carregado")

st.markdown("---")
with st.expander("Projecao de Profundidade Final (opcional)"):
    md_final = st.number_input("Profundidade Final (MD) desejada (m)", value=0.0)
    tipo_projecao_ui = st.radio("Parametros de projecao",
                                 ["Continuar com mesma Inclinacao e Azimute da ultima estacao", "Usar novos valores de Inclinacao e Azimute"])
    nova_incl = st.number_input("Nova Inclinacao (graus)", value=0.0, disabled=not tipo_projecao_ui.startswith("Usar"))
    novo_azim = st.number_input("Novo Azimute (graus)", value=0.0, disabled=not tipo_projecao_ui.startswith("Usar"))

    cp1, cp2 = st.columns(2)
    if cp1.button("Calcular Projecao"):
        if st.session_state.data_executado is None:
            st.error("Carregue primeiro o poco executado!")
        elif md_final <= 0:
            st.warning("Digite uma profundidade final valida (maior que 0).")
        else:
            try:
                tipo_sel = "mesmos" if not tipo_projecao_ui.startswith("Usar") else "novos"
                proj = projetar_profundidade_final(
                    st.session_state.data_executado, st.session_state.tipo_executado, md_final, tipo_sel,
                    nova_incl, novo_azim, st.session_state.mesa_rotativa,
                    st.session_state.coord_origem_x, st.session_state.coord_origem_y
                )
                st.session_state.data_projecao = proj
                st.success(f"Projecao calculada com {len(proj)} pontos.")
            except Exception as e:
                st.error(str(e))
    if cp2.button("Limpar Projecao"):
        st.session_state.data_projecao = None
        st.info("Projecao removida.")

st.markdown("---")
st.subheader("Parametros do Grafico")
cg1, cg2, cg3 = st.columns(3)
raio_tol_vert = cg1.number_input("Raio de Tolerancia Vertical (m)", value=0.0)
raio_tol_horiz = cg2.number_input("Raio de Tolerancia Horizontal (m)", value=0.0)
tipo_prof = cg3.selectbox("Tipo de Profundidade (elipse)", ["TVD", "Cota"])

cg4, cg5 = st.columns(2)
profundidade_elipse = cg4.number_input("Profundidade para Elipse (m)", value=1000.0)
intervalo_distancia = cg5.number_input("Intervalo de calculo da distancia ao longo do executado (m)", value=5.0, min_value=0.1)

if st.button("Plotar Trajetoria", type="primary", use_container_width=True):
    if st.session_state.data_planejado is None and st.session_state.data_executado is None:
        st.warning("Carregue pelo menos um arquivo (planejado ou executado) antes de plotar.")
    else:
        fig, relatorio_csv, avisos = gerar_trajetoria(
            st.session_state.data_planejado, st.session_state.tipo_planejado,
            st.session_state.data_executado, st.session_state.tipo_executado,
            raio_tol_vert, raio_tol_horiz, tipo_prof, profundidade_elipse,
            st.session_state.data_sidetrack, st.session_state.tipo_sidetrack,
            intervalo_distancia, st.session_state.data_projecao,
            st.session_state.mesa_rotativa, st.session_state.coord_origem_x, st.session_state.coord_origem_y
        )
        st.session_state.fig = fig
        st.session_state.relatorio_csv = relatorio_csv
        for a in avisos:
            st.info(a)

if st.session_state.fig is not None:
    html_grafico = pio.to_html(
        st.session_state.fig, full_html=True, include_plotlyjs=True,
        post_script=CLICK_JS, div_id="grafico-trajetoria"
    )
    import streamlit.components.v1 as components
    components.html(html_grafico, height=900, scrolling=True)

    dl1, dl2 = st.columns(2)
    dl1.download_button("Baixar grafico (HTML interativo)", data=html_grafico,
                          file_name="grafico_trajetoria.html", mime="text/html", use_container_width=True)
    dl2.download_button("Baixar relatorio (CSV)", data=st.session_state.relatorio_csv,
                          file_name="relatorio_trajetoria.csv", mime="text/csv", use_container_width=True)
