"""Conciliación bancaria vs. facturas de Siigo — herramienta interna, un solo usuario."""
from __future__ import annotations

import io
import os
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import dotenv_values, load_dotenv

from matching import reconcile
from siigo_client import SiigoAPIError, SiigoAuthError, SiigoClient, nombre_tercero, normalize_document

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)


def guardar_credenciales(username: str, access_key: str, partner_id: str):
    actuales = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    actuales.update(
        {"SIIGO_USERNAME": username, "SIIGO_ACCESS_KEY": access_key, "SIIGO_PARTNER_ID": partner_id}
    )
    ENV_PATH.write_text("".join(f"{k}={v}\n" for k, v in actuales.items()))


def parse_monto_cop(valor) -> float | None:
    """Convierte valores tipo 'COP -$ 37.840,00' (formato colombiano) a float."""
    if pd.isna(valor):
        return None
    s = str(valor)
    negativo = "-" in s
    s = re.sub(r"[^0-9.,]", "", s)
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        num = float(s)
    except ValueError:
        return None
    return -num if negativo else num


def parse_monto_columna(serie: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(serie):
        return pd.to_numeric(serie, errors="coerce")
    return serie.apply(parse_monto_cop)


def indice_columna(columnas: list, candidatos: list[str]) -> int:
    for candidato in candidatos:
        for i, c in enumerate(columnas):
            if str(c).strip().lower() == candidato:
                return i
    return 0


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_ventas_cached(_client, date_start: str, date_end: str, _on_page=None):
    return _client.get_sales_invoices(date_start, date_end, on_page=_on_page)


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_compras_cached(_client, _on_page=None):
    # Siempre trae TODO el historial de compras (la API de Siigo no filtra por fecha aquí) —
    # por eso vale la pena cachearlo: solo se paga el costo completo una vez.
    return _client.get_purchases(on_page=_on_page)


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_soporte_cached(_client, _on_page=None):
    return _client.get_purchase_support_documents(on_page=_on_page)


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_nombre_tercero_cached(_client, tercero_id: str) -> str:
    try:
        return nombre_tercero(_client.get_customer(tercero_id))
    except SiigoAPIError:
        return ""


APP_PASSWORD = os.getenv("APP_PASSWORD", "cambiar-esta-clave")

st.set_page_config(page_title="Conciliación bancaria — Siigo", layout="wide")

if not st.session_state.get("autenticado"):
    st.title("Conciliación bancaria — acceso")
    clave = st.text_input("Clave de acceso", type="password")
    if st.button("Entrar"):
        if clave == APP_PASSWORD:
            st.session_state["autenticado"] = True
            st.rerun()
        else:
            st.error("Clave incorrecta")
    st.stop()

st.title("Conciliación bancaria vs. facturas Siigo")
st.caption("Sube el Excel del banco, trae las facturas de Siigo por API, y revisa qué quedó conciliado.")

with st.sidebar:
    if st.button("Cerrar sesión"):
        st.session_state["autenticado"] = False
        st.rerun()
    st.header("Credenciales Siigo")
    username = st.text_input("Usuario Siigo", value=os.getenv("SIIGO_USERNAME", ""))
    access_key = st.text_input("Access key", value=os.getenv("SIIGO_ACCESS_KEY", ""), type="password")
    partner_id = st.text_input("Partner-Id", value=os.getenv("SIIGO_PARTNER_ID", ""))
    st.caption("La access key se genera en Siigo Nube → Alianzas → Mi credencial API.")

    col_a, col_b = st.columns(2)
    with col_a:
        probar = st.button("Probar conexión")
    with col_b:
        guardar = st.button("Guardar conexión")

    if probar or guardar:
        if not username or not access_key or not partner_id:
            st.error("Completa usuario, access key y Partner-Id primero.")
        else:
            try:
                with st.spinner("Conectando con Siigo..."):
                    SiigoClient(username, access_key, partner_id).authenticate()
                st.success("Conectado correctamente con Siigo ✅")
                if guardar:
                    guardar_credenciales(username, access_key, partner_id)
                    st.success("Guardado. La próxima vez que abras la app, ya vas a estar conectado.")
            except SiigoAuthError as e:
                st.error(f"No se pudo conectar: {e}")

    st.header("Periodo a conciliar")
    hoy = date.today()
    fecha_inicio = st.date_input("Desde", value=hoy - timedelta(days=30))
    fecha_fin = st.date_input("Hasta", value=hoy)

    tolerancia = st.slider(
        "Tolerancia de días entre fecha banco y fecha factura",
        0,
        180,
        30,
        help="Las facturas de venta rara vez se pagan el mismo día — súbelo a 30-60 días si "
        "tus clientes compran a crédito. Si hay varias facturas candidatas en la ventana, "
        "la app intenta desempatar con el número de factura o documento del proveedor "
        "antes de marcarlo como ambiguo.",
    )

st.subheader("1. Movimientos bancarios")
archivos = st.file_uploader(
    "Excel(es) del banco (.xlsx) — puedes subir varios meses a la vez",
    type=["xlsx"],
    accept_multiple_files=True,
)

bank_df = None
if archivos:
    partes = []
    for f in archivos:
        parte = pd.read_excel(f)
        parte["Archivo"] = f.name
        partes.append(parte)

    columnas_primer_archivo = set(partes[0].columns) - {"Archivo"}
    for f, parte in zip(archivos, partes):
        if set(parte.columns) - {"Archivo"} != columnas_primer_archivo:
            st.warning(f"'{f.name}' tiene columnas distintas a los demás — revisa que sea el mismo formato de extracto.")

    raw = pd.concat(partes, ignore_index=True)
    st.caption(f"{len(archivos)} archivo(s) cargados · {len(raw)} filas en total.")
    st.dataframe(raw.head(10), use_container_width=True)

    columnas = [c for c in raw.columns if c != "Archivo"]
    idx_fecha = indice_columna(columnas, ["fecha"])
    idx_desc = indice_columna(columnas, ["descripción", "descripcion"])
    idx_valor = indice_columna(columnas, ["valor"])

    col1, col2, col3 = st.columns(3)
    with col1:
        col_fecha = st.selectbox("Columna de fecha", columnas, index=idx_fecha)
    with col2:
        col_desc = st.selectbox("Columna de descripción/referencia", columnas, index=idx_desc)
    with col3:
        modo_monto = st.radio(
            "Formato del monto",
            ["Una columna (signo +/-)", "Columnas separadas débito/crédito"],
            help="Bancolombia y similares traen una sola columna 'Valor' con el signo incluido.",
        )

    raw[col_fecha] = pd.to_datetime(raw[col_fecha], errors="coerce").dt.date

    cols_referencia = st.multiselect(
        "Columna(s) de referencia adicional (opcional)",
        [c for c in columnas if c not in (col_fecha, col_desc)],
        help="Si el banco trae el número de documento del proveedor en una columna aparte "
        "(ej. 'Referencia 1'), inclúyela aquí para que la app la use al conciliar.",
    )

    if modo_monto == "Una columna (signo +/-)":
        col_monto = st.selectbox("Columna de monto", columnas, index=idx_valor)
        tmp = raw[[col_fecha, col_desc, col_monto]].copy()
        tmp.columns = ["fecha", "descripcion", "monto"]
        tmp["monto"] = parse_monto_columna(tmp["monto"])
    else:
        c1, c2 = st.columns(2)
        with c1:
            col_debito = st.selectbox("Columna de débitos (egresos)", columnas)
        with c2:
            col_credito = st.selectbox("Columna de créditos (ingresos)", columnas)
        tmp = raw[[col_fecha, col_desc, col_debito, col_credito]].copy()
        tmp.columns = ["fecha", "descripcion", "debito", "credito"]
        tmp["debito"] = parse_monto_columna(tmp["debito"]).fillna(0)
        tmp["credito"] = parse_monto_columna(tmp["credito"]).fillna(0)
        tmp["monto"] = tmp["credito"] - tmp["debito"]
        tmp = tmp[["fecha", "descripcion", "monto"]]

    for c in cols_referencia:
        tmp["descripcion"] = tmp["descripcion"].astype(str) + " | " + raw.loc[tmp.index, c].astype(str)

    tmp["fecha"] = pd.to_datetime(tmp["fecha"], errors="coerce")
    bank_df = tmp.dropna(subset=["fecha", "monto"]).reset_index().rename(columns={"index": "fila_original"})
    st.caption(f"{len(bank_df)} de {len(raw)} movimientos leídos correctamente.")
    vista_previa = bank_df.drop(columns="fila_original").head(10).copy()
    vista_previa["fecha"] = vista_previa["fecha"].dt.date
    st.dataframe(vista_previa, use_container_width=True)

st.subheader("2. Conciliar")
if st.button("Conciliar con Siigo", type="primary", disabled=bank_df is None):
    if bank_df is None:
        st.warning("Primero sube el Excel del banco arriba y espera a que se lea.")
        st.stop()
    if not username or not access_key or not partner_id:
        st.error("Completa usuario, access key y Partner-Id de Siigo en la barra lateral.")
        st.stop()

    rango_inicio = min(
        pd.Timestamp(fecha_inicio), bank_df["fecha"].min() - pd.Timedelta(days=tolerancia)
    )
    rango_fin = max(pd.Timestamp(fecha_fin), bank_df["fecha"].max())
    if rango_inicio < pd.Timestamp(fecha_inicio) or rango_fin > pd.Timestamp(fecha_fin):
        st.info(
            f"Se amplió automáticamente la consulta a Siigo a {rango_inicio.date()} – {rango_fin.date()} "
            "para cubrir tus movimientos y la tolerancia de días configurada (facturas a crédito)."
        )

    client = SiigoClient(username, access_key, partner_id)
    progreso = st.empty()
    try:
        ds, de = rango_inicio.date().isoformat(), rango_fin.date().isoformat()
        progreso.text("Descargando facturas de venta...")
        ventas_raw = _fetch_ventas_cached(
            client, ds, de, _on_page=lambda n, total: progreso.text(f"Descargando facturas de venta... {n}/{total}")
        )
        progreso.text("Descargando facturas de compra (todo el historial, puede tardar la primera vez)...")
        compras_raw = _fetch_compras_cached(
            client, _on_page=lambda n, total: progreso.text(f"Descargando facturas de compra... {n}/{total}")
        )
        progreso.text("Descargando documentos soporte...")
        soporte_raw = _fetch_soporte_cached(
            client, _on_page=lambda n, total: progreso.text(f"Descargando documentos soporte... {n}/{total}")
        )
        progreso.empty()
    except SiigoAuthError as e:
        progreso.empty()
        st.error(f"No se pudo autenticar con Siigo: {e}")
        st.stop()
    except SiigoAPIError as e:
        progreso.empty()
        st.error(f"Error consultando la API de Siigo: {e}")
        st.stop()

    with st.expander("Detalle técnico (para depurar)"):
        st.write(f"Facturas de venta descargadas: {len(ventas_raw)}")
        st.write(f"Facturas de compra descargadas (antes de filtrar por fecha): {len(compras_raw)}")
        st.write(f"Documentos soporte descargados (antes de filtrar por fecha): {len(soporte_raw)}")
        if compras_raw:
            st.write("Ejemplo de factura de compra cruda:")
            st.json(compras_raw[0])
        else:
            st.warning(
                "Siigo no devolvió NINGUNA factura de compra en tu cuenta. "
                "Puede ser que no tengas compras registradas en Siigo, o que el usuario/access key "
                "no tenga permiso sobre ese módulo. No es un problema de fechas: este endpoint trae todo."
            )

    ventas = pd.DataFrame([normalize_document(d, "venta") for d in ventas_raw])
    compras = pd.DataFrame(
        [normalize_document(d, "compra") for d in compras_raw]
        + [normalize_document(d, "soporte") for d in soporte_raw]
    )

    for df in (ventas, compras):
        if not df.empty:
            df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
            df["total"] = pd.to_numeric(df["total"], errors="coerce")

    if not ventas.empty:
        ventas = ventas[(ventas["fecha"] >= rango_inicio) & (ventas["fecha"] <= rango_fin)].reset_index(drop=True)
    if not compras.empty:
        compras = compras[(compras["fecha"] >= rango_inicio) & (compras["fecha"] <= rango_fin)].reset_index(drop=True)

    terceros_unicos = pd.concat([ventas.get("tercero_id", pd.Series(dtype=str)), compras.get("tercero_id", pd.Series(dtype=str))])
    terceros_unicos = [t for t in terceros_unicos.unique() if t]
    cache_nombres = {}
    if terceros_unicos:
        barra = st.progress(0.0, text=f"Consultando nombres de clientes/proveedores... 0/{len(terceros_unicos)}")
        for n, tid in enumerate(terceros_unicos, start=1):
            cache_nombres[tid] = _fetch_nombre_tercero_cached(client, tid)
            barra.progress(n / len(terceros_unicos), text=f"Consultando nombres de clientes/proveedores... {n}/{len(terceros_unicos)}")
        barra.empty()
    for df in (ventas, compras):
        if not df.empty:
            df["tercero_nombre"] = df["tercero_id"].map(cache_nombres).fillna("")

    ingresos_banco = bank_df[bank_df["monto"] > 0].copy()
    egresos_banco = bank_df[bank_df["monto"] < 0].copy()
    egresos_banco["monto"] = egresos_banco["monto"].abs()

    vacio_bank = lambda df: df.assign(conciliado=False, ambiguo=False, documento_sugerido="", tipo_documento_sugerido="", tercero_sugerido="", documento_proveedor_sugerido="", diferencia_dias=pd.NA)
    with st.spinner("Conciliando movimientos..."):
        bank_ventas, docs_ventas = reconcile(ingresos_banco, ventas, tolerancia) if not ventas.empty else (vacio_bank(ingresos_banco), ventas)
        bank_compras, docs_compras = reconcile(egresos_banco, compras, tolerancia) if not compras.empty else (vacio_bank(egresos_banco), compras)

    bank_ventas["tipo"] = "ingreso"
    bank_compras["tipo"] = "egreso"

    resultado = pd.concat([bank_ventas, bank_compras], ignore_index=True).sort_values("fila_original").reset_index(drop=True)

    n_ambiguos = int(resultado["ambiguo"].sum())
    st.success(
        f"{int(resultado['conciliado'].sum())}/{len(resultado)} movimientos bancarios conciliados con una factura sugerida"
        + (f" · ⚠️ {n_ambiguos} con más de una factura candidata, revísalos" if n_ambiguos else "")
    )

    COL_FV = "Factura de venta sugerida (FV)"
    COL_FC = "Factura de compra sugerida (FC)"
    COL_TERCERO = "Cliente/Proveedor sugerido"
    COL_DOC_PROV = "Documento proveedor"
    COL_REVISAR = "Revisar"
    salida = raw.copy()
    salida[COL_FV] = ""
    salida[COL_FC] = ""
    salida[COL_TERCERO] = ""
    salida[COL_DOC_PROV] = ""
    salida[COL_REVISAR] = ""
    fv_map = resultado[resultado["tipo"] == "ingreso"].set_index("fila_original")["documento_sugerido"]
    fc_map = resultado[resultado["tipo"] == "egreso"].set_index("fila_original")["documento_sugerido"]
    tercero_map = resultado.set_index("fila_original")["tercero_sugerido"]
    doc_prov_map = resultado.set_index("fila_original")["documento_proveedor_sugerido"]
    ambiguo_map = resultado.set_index("fila_original")["ambiguo"]
    salida.loc[fv_map.index, COL_FV] = fv_map
    salida.loc[fc_map.index, COL_FC] = fc_map
    salida.loc[tercero_map.index, COL_TERCERO] = tercero_map
    salida.loc[doc_prov_map.index, COL_DOC_PROV] = doc_prov_map
    salida.loc[ambiguo_map[ambiguo_map].index, COL_REVISAR] = "⚠️ Varias facturas coinciden en monto y fecha"

    st.subheader("3. Extracto bancario con factura sugerida (FV/FC)")
    st.caption(
        "Mismo Excel del banco, con columnas nuevas al final. "
        "Puedes editar directamente FV, FC y Cliente/Proveedor antes de descargar — "
        "el archivo se genera con tus correcciones."
    )
    columnas_editables = [COL_FV, COL_FC, COL_TERCERO, COL_DOC_PROV]
    salida_editada = st.data_editor(
        salida,
        use_container_width=True,
        disabled=[c for c in salida.columns if c not in columnas_editables],
        key="editor_salida",
    )

    with st.expander("Facturas de Siigo sin ningún movimiento bancario asociado"):
        st.write("Facturas de venta:")
        st.dataframe(docs_ventas[~docs_ventas["conciliado"]] if not docs_ventas.empty else docs_ventas, use_container_width=True)
        st.write("Facturas/documentos de compra:")
        st.dataframe(docs_compras[~docs_compras["conciliado"]] if not docs_compras.empty else docs_compras, use_container_width=True)

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        salida_editada.to_excel(writer, sheet_name="Extracto + Siigo", index=False)
        docs_ventas.to_excel(writer, sheet_name="Ventas sin conciliar", index=False)
        docs_compras.to_excel(writer, sheet_name="Compras sin conciliar", index=False)
    st.download_button(
        "Descargar resultado en Excel",
        data=buffer.getvalue(),
        file_name=f"conciliacion_{rango_inicio.date()}_{rango_fin.date()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
