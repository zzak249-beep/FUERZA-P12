import json


def load_state(path):
    """Carga solo entradas propias (dict con 'side' y 'sl_id'); ignora cualquier otro contenido del archivo."""
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict) and "side" in v and "sl_id" in v}
