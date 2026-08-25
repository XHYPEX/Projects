import json
import logging
import os

import config

logger = logging.getLogger(__name__)


def load_message_map() -> dict[tuple[int, int], int]:
    """Key: (source_chat_id, source_message_id) -> ID pesan yang di-post di channel tujuan.
    Composite key dipakai supaya ID pesan gak tabrakan antar grup sumber (multi-route)."""
    if not os.path.exists(config.MESSAGE_MAP_PATH):
        return {}
    try:
        with open(config.MESSAGE_MAP_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result = {}
        for k, v in raw.items():
            chat_id_str, msg_id_str = k.split(":", 1)
            result[(int(chat_id_str), int(msg_id_str))] = v
        return result
    except Exception:
        logger.exception("Gagal load %s, mulai dari mapping kosong.", config.MESSAGE_MAP_PATH)
        return {}


def save_message_map(message_map: dict[tuple[int, int], int]) -> None:
    # Prune entri terlama kalau kelebihan, supaya file gak membengkak tanpa batas
    # selama bot jalan berbulan-bulan.
    while len(message_map) > config.MESSAGE_MAP_MAX_ENTRIES:
        oldest_key = next(iter(message_map))
        del message_map[oldest_key]

    tmp_path = config.MESSAGE_MAP_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({f"{k[0]}:{k[1]}": v for k, v in message_map.items()}, f)
        os.replace(tmp_path, config.MESSAGE_MAP_PATH)
    except Exception:
        logger.exception("Gagal simpan %s.", config.MESSAGE_MAP_PATH)
