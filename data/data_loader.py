# =============================================================================
# QUANTLAB — FASE 1
# Módulo: data/data_loader.py
# Propósito: Core Market Data Engine
# Instrumento: NQ/MNQ Futures
# =============================================================================

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


# -----------------------------------------------------------------------------
# SECCIÓN 1: GENERADOR DE DATOS SINTÉTICOS
# -----------------------------------------------------------------------------
# ¿Por qué datos sintéticos?
# No tenemos tick data real todavía. Generamos OHLCV sintético que simula
# el comportamiento real de NQ: tendencia, volatilidad, volumen por sesión.
# Esto nos permite construir y validar todos los motores antes de conectar
# datos reales.

def generate_synthetic_nq(
    start_date: str = "2024-01-02",
    days: int = 20,
    freq: str = "1min",
    base_price: float = 17000.0,
    volatility: float = 0.0003,
    seed: int = 42
) -> pd.DataFrame:
    """
    Genera datos OHLCV sintéticos que simulan NQ Futures.

    Parámetros:
        start_date : fecha de inicio (string YYYY-MM-DD)
        days       : número de días de trading a generar
        freq       : frecuencia de las velas ('1min', '5min', etc.)
        base_price : precio inicial de NQ
        volatility : volatilidad por barra (0.0003 = 0.03%)
        seed       : semilla para reproducibilidad

    Retorna:
        DataFrame con columnas: datetime, open, high, low, close, volume
    """

    np.random.seed(seed)

    # --- Construir rango de timestamps solo en horario de mercado ---
    # NQ opera prácticamente 24h pero el volumen real está en NY session
    # Para esta fase usamos horario regular: 09:30 - 16:00 NY
    all_timestamps = []
    current_date = pd.Timestamp(start_date)

    for _ in range(days):
        # Saltar fines de semana
        if current_date.weekday() < 5:  # 0=Lunes, 4=Viernes
            session_times = pd.date_range(
                start=current_date.replace(hour=9, minute=30),
                end=current_date.replace(hour=16, minute=0),
                freq=freq
            )
            all_timestamps.extend(session_times)
        current_date += timedelta(days=1)

    n_bars = len(all_timestamps)

    # --- Simular precio con random walk + drift alcista leve ---
    # Random walk: precio[t] = precio[t-1] * (1 + retorno_aleatorio)
    # Drift: tendencia alcista pequeña para simular NQ realista
    drift = 0.00002  # drift positivo muy pequeño por barra
    returns = np.random.normal(drift, volatility, n_bars)
    
    # Precio de cierre acumulado
    close_prices = base_price * np.cumprod(1 + returns)

    # --- Construir OHLCV desde close ---
    # Cada vela: open = close anterior, high/low son desviaciones del close
    opens = np.roll(close_prices, 1)  # desplazar un período
    opens[0] = base_price             # primer open = precio base

    # High y Low: desviación aleatoria proporcional a la volatilidad
    high_offset = np.abs(np.random.normal(0, volatility * base_price, n_bars))
    low_offset  = np.abs(np.random.normal(0, volatility * base_price, n_bars))

    highs = np.maximum(opens, close_prices) + high_offset
    lows  = np.minimum(opens, close_prices) - low_offset

    # --- Volumen sintético ---
    # Volumen de NQ por minuto: base ~800, con spikes aleatorios
    # Mayor volumen al open (09:30-10:00) y al close (15:30-16:00)
    base_volume = np.random.lognormal(mean=6.5, sigma=0.5, size=n_bars).astype(int)

    # Construir DataFrame
    df = pd.DataFrame({
        "datetime": all_timestamps,
        "open":     np.round(opens, 2),
        "high":     np.round(highs, 2),
        "low":      np.round(lows, 2),
        "close":    np.round(close_prices, 2),
        "volume":   base_volume
    })

    df.set_index("datetime", inplace=True)

    return df


# -----------------------------------------------------------------------------
# SECCIÓN 2: VALIDACIÓN DE DATOS
# -----------------------------------------------------------------------------
# Antes de usar cualquier dato (sintético o real), validamos integridad.
# Reglas básicas de OHLCV que siempre deben cumplirse.

def validate_ohlcv(df: pd.DataFrame) -> bool:
    """
    Valida que el DataFrame cumpla reglas básicas de integridad OHLCV.

    Reglas:
        1. Columnas requeridas presentes
        2. Sin valores nulos
        3. High >= Low en todas las barras
        4. High >= Open y High >= Close
        5. Low  <= Open y Low  <= Close
        6. Volume >= 0

    Retorna:
        True si pasa todas las validaciones, lanza error si no.
    """

    required_columns = ["open", "high", "low", "close", "volume"]

    # Regla 1: columnas presentes
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Columnas faltantes: {missing}")

    # Regla 2: sin nulos
    null_counts = df[required_columns].isnull().sum()
    if null_counts.any():
        raise ValueError(f"Valores nulos detectados:\n{null_counts[null_counts > 0]}")

    # Regla 3-5: consistencia de OHLC
    invalid_bars = df[
        (df["high"] < df["low"]) |
        (df["high"] < df["open"]) |
        (df["high"] < df["close"]) |
        (df["low"]  > df["open"]) |
        (df["low"]  > df["close"])
    ]
    if len(invalid_bars) > 0:
        raise ValueError(f"Barras OHLC inválidas: {len(invalid_bars)} encontradas")

    # Regla 6: volumen no negativo
    if (df["volume"] < 0).any():
        raise ValueError("Volumen negativo detectado")

    print(f"✓ Validación pasada — {len(df)} barras, sin errores")
    return True


# -----------------------------------------------------------------------------
# SECCIÓN 3: SEGMENTACIÓN POR SESIÓN
# -----------------------------------------------------------------------------
# Uno de los conceptos más importantes en order flow profesional:
# el mercado NO se comporta igual en todas las horas.
# Segmentar por sesión nos permite analizar cada ventana por separado.

def add_session_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega una columna 'session' que clasifica cada barra por ventana horaria.

    Sesiones definidas (hora NY / Eastern Time):
        pre_market   : 04:00 - 09:29
        open         : 09:30 - 10:30  ← alta volatilidad, order flow agresivo
        mid_morning  : 10:31 - 12:00
        lunch        : 12:01 - 13:30  ← volumen bajo, evitar
        afternoon    : 13:31 - 16:00  ← segunda ventana relevante
        after_hours  : 16:01 - 20:00

    Nota: NQ opera casi 24h. Para esta fase solo clasificamos las sesiones
    principales. Sesiones fuera de rango quedan como 'overnight'.
    """

    df = df.copy()

    # Extraer hora como número decimal para comparar fácilmente
    # Ejemplo: 09:30 → 9.5, 13:45 → 13.75
    hour_decimal = df.index.hour + df.index.minute / 60

    conditions = [
        (hour_decimal >= 4.0)  & (hour_decimal < 9.5),
        (hour_decimal >= 9.5)  & (hour_decimal < 10.5),
        (hour_decimal >= 10.5) & (hour_decimal < 12.0),
        (hour_decimal >= 12.0) & (hour_decimal < 13.5),
        (hour_decimal >= 13.5) & (hour_decimal < 16.0),
        (hour_decimal >= 16.0) & (hour_decimal < 20.0),
    ]

    labels = [
        "pre_market",
        "open",
        "mid_morning",
        "lunch",
        "afternoon",
        "after_hours"
    ]

    df["session"] = np.select(conditions, labels, default="overnight")

    return df


# -----------------------------------------------------------------------------
# SECCIÓN 4: RESAMPLING
# -----------------------------------------------------------------------------
# Convertir timeframe base (1min) a timeframes más altos (5min, 15min, etc.)
# Regla OHLCV para resampling:
#   Open  = primer open del período
#   High  = máximo de todos los highs
#   Low   = mínimo de todos los lows
#   Close = último close del período
#   Volume = suma de todos los volúmenes

def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    Resamplea datos OHLCV a un timeframe mayor.

    Parámetros:
        df        : DataFrame con datos en timeframe base
        timeframe : string de frecuencia pandas ('5min', '15min', '1h', etc.)

    Retorna:
        DataFrame resampleado y limpio
    """

    resampled = df.resample(timeframe).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum"
    })

    # Eliminar barras vacías (períodos sin datos, fuera de sesión)
    resampled.dropna(inplace=True)

    # Re-agregar labels de sesión si existían
    if "session" in df.columns:
        resampled = add_session_labels(resampled)

    return resampled


# -----------------------------------------------------------------------------
# SECCIÓN 5: ENTRY POINT — TEST DEL MÓDULO
# -----------------------------------------------------------------------------

if __name__ == "__main__":

    print("=" * 60)
    print("QUANTLAB — Fase 1: Core Market Data Engine")
    print("=" * 60)

    # 1. Generar datos sintéticos
    print("\n[1] Generando datos sintéticos NQ (20 días, 1min)...")
    df = generate_synthetic_nq(days=20)
    print(f"    Barras generadas : {len(df)}")
    print(f"    Rango de fechas  : {df.index[0]} → {df.index[-1]}")
    print(f"    Precio inicial   : {df['close'].iloc[0]:.2f}")
    print(f"    Precio final     : {df['close'].iloc[-1]:.2f}")

    # 2. Validar datos
    print("\n[2] Validando integridad OHLCV...")
    validate_ohlcv(df)

    # 3. Agregar sesiones
    print("\n[3] Segmentando por sesión...")
    df = add_session_labels(df)
    session_counts = df["session"].value_counts()
    print(f"    Distribución de barras por sesión:")
    for session, count in session_counts.items():
        print(f"    {session:<15} : {count} barras")

    # 4. Resamplear a 5 minutos
    print("\n[4] Resampleando a 5min...")
    df_5min = resample_ohlcv(df, "5min")
    print(f"    Barras 1min  : {len(df)}")
    print(f"    Barras 5min  : {len(df_5min)}")

    # 5. Preview de los datos
    print("\n[5] Preview de primeras 5 barras (1min):")
    print(df.head())

    print("\n[6] Preview de primeras 5 barras (5min):")
    print(df_5min.head())

    print("\n" + "=" * 60)
    print("Fase 1 completa. Data Engine operativo.")
    print("=" * 60)