"""Common loaders for Round 5 data."""
from __future__ import annotations
import os
import pandas as pd

THEMES = {
    "PEBBLES":      ["XS", "S", "M", "L", "XL"],
    "PANEL":        ["1X2", "1X4", "2X2", "2X4", "4X4"],
    "SNACKPACK":    ["CHOCOLATE", "PISTACHIO", "RASPBERRY", "STRAWBERRY", "VANILLA"],
    "MICROCHIP":    ["CIRCLE", "OVAL", "RECTANGLE", "SQUARE", "TRIANGLE"],
    "SLEEP_POD":    ["COTTON", "LAMB_WOOL", "NYLON", "POLYESTER", "SUEDE"],
    "OXYGEN_SHAKE": ["CHOCOLATE", "EVENING_BREATH", "GARLIC", "MINT", "MORNING_BREATH"],
    "GALAXY_SOUNDS":["BLACK_HOLES", "DARK_MATTER", "PLANETARY_RINGS", "SOLAR_FLAMES", "SOLAR_WINDS"],
    "TRANSLATOR":   ["ASTRO_BLACK", "ECLIPSE_CHARCOAL", "GRAPHITE_MIST", "SPACE_GRAY", "VOID_BLUE"],
    "UV_VISOR":     ["AMBER", "MAGENTA", "ORANGE", "RED", "YELLOW"],
    "ROBOT":        ["DISHES", "IRONING", "LAUNDRY", "MOPPING", "VACUUMING"],
}

ALL_PRODUCTS = [f"{t}_{m}" for t, ms in THEMES.items() for m in ms]
PROD_TO_THEME = {f"{t}_{m}": t for t, ms in THEMES.items() for m in ms}

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def load_prices(days=(2, 3, 4)) -> pd.DataFrame:
    dfs = []
    for d in days:
        path = os.path.join(DATA_DIR, f"prices_round_5_day_{d}.csv")
        df = pd.read_csv(path, sep=";")
        dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)
    df["global_ts"] = df["day"] * 1_000_000 + df["timestamp"]
    return df


def load_trades(days=(2, 3, 4)) -> pd.DataFrame:
    dfs = []
    for d in days:
        path = os.path.join(DATA_DIR, f"trades_round_5_day_{d}.csv")
        df = pd.read_csv(path, sep=";")
        df["day"] = d
        dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)
    df["global_ts"] = df["day"] * 1_000_000 + df["timestamp"]
    return df


def mid_panel(prices: pd.DataFrame) -> pd.DataFrame:
    """Pivot to a wide panel: rows = global_ts, columns = product, values = mid_price."""
    return prices.pivot_table(index="global_ts", columns="product", values="mid_price", aggfunc="first").sort_index()
