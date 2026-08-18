"""Motor de conciliación: cruza movimientos bancarios con documentos de Siigo por monto y fecha cercana."""
import itertools
import re

import pandas as pd


def _normalizar_texto(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _buscar_combinacion(valores: pd.Series, objetivo: float, max_combo: int = 3, max_candidatos: int = 12):
    """Busca un subconjunto (tamaño 2..max_combo) de `valores` (Series indice->monto)
    cuya suma coincida con `objetivo`. Devuelve la lista de índices o None.

    Si hay demasiados candidatos cercanos, se salta la búsqueda: probar todas las
    combinaciones sería lentísimo (y encontrar una "coincidencia" entre cientos de
    candidatos por azar ya no sería confiable de todos modos)."""
    indices = list(valores.index)
    if len(indices) < 2 or len(indices) > max_candidatos:
        return None
    for tam in range(2, min(max_combo, len(indices)) + 1):
        for combo in itertools.combinations(indices, tam):
            if round(sum(valores[i] for i in combo), 2) == round(objetivo, 2):
                return list(combo)
    return None


def reconcile(bank_df: pd.DataFrame, docs_df: pd.DataFrame, tolerance_days: int = 3):
    """
    bank_df: columnas ['fecha' (datetime), 'descripcion', 'monto' (float, positivo)]
    docs_df: columnas ['tipo', 'id', 'numero', 'fecha' (datetime), 'total' (float), 'tercero', 'tercero_nombre'?]

    Devuelve (bank_result, docs_result) con columna 'conciliado' y el cruce anotado.
    Primero busca coincidencias 1 a 1 por monto exacto; luego, para lo que quede sin
    conciliar, busca combinaciones (una transferencia que cubre varias facturas, o una
    factura pagada en varias cuotas).
    """
    bank = bank_df.reset_index(drop=True).copy()
    bank["conciliado"] = False
    bank["ambiguo"] = False
    bank["documento_sugerido"] = ""
    bank["tipo_documento_sugerido"] = ""
    bank["tercero_sugerido"] = ""
    bank["documento_proveedor_sugerido"] = ""
    bank["diferencia_dias"] = pd.NA

    docs = docs_df.reset_index(drop=True).copy()
    docs["conciliado"] = False
    docs["movimiento_match"] = ""

    # Paso 1: coincidencia 1 a 1 por monto exacto y fecha más cercana.
    for i, mov in bank.iterrows():
        candidatos = docs[(~docs["conciliado"]) & (docs["total"].round(2) == round(mov["monto"], 2))]
        if candidatos.empty:
            continue
        candidatos = candidatos.copy()
        candidatos["dist_dias"] = (candidatos["fecha"] - mov["fecha"]).dt.days.abs()
        candidatos = candidatos[candidatos["dist_dias"] <= tolerance_days]
        if candidatos.empty:
            continue

        best_idx = candidatos["dist_dias"].idxmin()
        ambiguo = len(candidatos) > 1
        if ambiguo:
            texto_mov = _normalizar_texto(mov["descripcion"])

            def _tiene_referencia(idx):
                refs = [candidatos.loc[idx, "numero"]]
                if "documento_proveedor" in candidatos.columns:
                    refs.append(candidatos.loc[idx, "documento_proveedor"])
                return any(ref and _normalizar_texto(ref) in texto_mov for ref in refs)

            coincidencias = [idx for idx in candidatos.index if _tiene_referencia(idx)]
            if len(coincidencias) == 1:
                best_idx = coincidencias[0]
                ambiguo = False

        docs.loc[best_idx, "conciliado"] = True
        docs.loc[best_idx, "movimiento_match"] = mov["descripcion"]
        bank.loc[i, "conciliado"] = True
        bank.loc[i, "ambiguo"] = ambiguo
        bank.loc[i, "documento_sugerido"] = docs.loc[best_idx, "numero"]
        bank.loc[i, "tipo_documento_sugerido"] = docs.loc[best_idx, "tipo"]
        bank.loc[i, "tercero_sugerido"] = docs.loc[best_idx].get("tercero_nombre", "")
        bank.loc[i, "documento_proveedor_sugerido"] = docs.loc[best_idx].get("documento_proveedor", "")
        bank.loc[i, "diferencia_dias"] = int(candidatos.loc[best_idx, "dist_dias"])

    # Paso 2a: varias facturas cubiertas por un solo movimiento bancario.
    for i, mov in bank[~bank["conciliado"]].iterrows():
        cercanos = docs[(~docs["conciliado"]) & ((docs["fecha"] - mov["fecha"]).dt.days.abs() <= tolerance_days)]
        combo = _buscar_combinacion(cercanos["total"], mov["monto"])
        if not combo:
            continue
        docs.loc[combo, "conciliado"] = True
        docs.loc[combo, "movimiento_match"] = mov["descripcion"]
        bank.loc[i, "conciliado"] = True
        bank.loc[i, "documento_sugerido"] = " + ".join(docs.loc[combo, "numero"])
        bank.loc[i, "tipo_documento_sugerido"] = docs.loc[combo[0], "tipo"]
        bank.loc[i, "tercero_sugerido"] = docs.loc[combo[0]].get("tercero_nombre", "")
        bank.loc[i, "documento_proveedor_sugerido"] = docs.loc[combo[0]].get("documento_proveedor", "")
        bank.loc[i, "diferencia_dias"] = int((docs.loc[combo, "fecha"] - mov["fecha"]).dt.days.abs().max())

    # Paso 2b: una factura pagada en varios movimientos (cuotas).
    for j, doc in docs[~docs["conciliado"]].iterrows():
        cercanos = bank[(~bank["conciliado"]) & ((bank["fecha"] - doc["fecha"]).dt.days.abs() <= tolerance_days)]
        combo = _buscar_combinacion(cercanos["monto"], doc["total"])
        if not combo:
            continue
        bank.loc[combo, "conciliado"] = True
        bank.loc[combo, "documento_sugerido"] = doc["numero"]
        bank.loc[combo, "tipo_documento_sugerido"] = doc["tipo"]
        bank.loc[combo, "tercero_sugerido"] = doc.get("tercero_nombre", "")
        bank.loc[combo, "documento_proveedor_sugerido"] = doc.get("documento_proveedor", "")
        bank.loc[combo, "diferencia_dias"] = (bank.loc[combo, "fecha"] - doc["fecha"]).dt.days.abs()
        docs.loc[j, "conciliado"] = True
        docs.loc[j, "movimiento_match"] = " + ".join(bank.loc[combo, "descripcion"])

    return bank, docs
