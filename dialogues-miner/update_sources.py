"""
update_sources.py — заполняет sources.json 118 чатами
и обновляет mining_config.json с фильтрами.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources.json"
CONFIG = ROOT / "configs" / "mining_config.json"

# ─── 118 чатов, разбитых по группам ───────────────────────────
CHANNELS = {
    "tech": [
        "K_3_D", "webprogrammists", "pro_osdev", "YandexCloud", "Zigbee",
        "CompilerDev", "itdogchat",
    ],
    "education": [
        "lessondeliverychat", "onlineShkoly", "botalkaaa",
        "repetitor_repetitori", "studentsfortutors",
    ],
    "psychology": [
        "PsiologWeHelp", "mirlru", "mentally_pie", "clean_cognitions",
        "proSVETlenieLav", "SHKOLAAVTONOMII",
    ],
    "marketplace": [
        "wbchatpostavshikov", "wildberries_markets", "avito_chatic",
        "wb_help", "wb_ozonchat", "mari_vakansiii", "lutacash",
        "marketplacechats_help", "avito_chats", "obyavaVolnovaha",
        "podrabotkarabotaspb", "spbpvz", "marketWildberriess",
        "tovarka_rossiya", "marketplace_chattt", "wbnahodkychat",
        "marketplaceessChat", "wildbernes_marketplace_ozonchat",
        "biznes_klient_rabota", "neBlackGroup2",
    ],
    "design": [
        "designphotomp", "blender_ru", "desgangchat", "Dizainerv_WB_Ozon",
        "Dizainerv_WBOzon", "onemarketservices", "dizainer_ozonwb",
        "models3DPrint", "DIZAINERTY T",
    ],
    "food": [
        "oficiant_moskva", "hostes_chat", "horecapersonal_chat",
        "konditer_chat", "fishmarket65", "shef_craba_perm",
        "biblioteka_konditera", "Shef_kraba_Kazan", "UrozhaiUlov",
        "gsd_recept", "oficianty_barmeny", "chat_konditer",
        "Shef_kraba_mgn", "shef_craba_chelyab", "forel_iz_karelii_tula",
        "toppovar", "shef_craba_nizhniy_novgorod", "Shef_kraba_izhevsk",
        "zapret_z4r", "barista_yuga", "produkty_pitaniya", "vvkusiteli",
        "recepty_kulinaria", "forum_myasnika", "konditery_pekari",
        "grandshef_chat", "images_for_konditery", "obshepit_yaroslavl",
        "mishka_bar", "ryba_na_dom_krym", "che_pochem_krasnoyarsk",
        "horeca_chat", "appetit_perm", "akadem_ferma",
    ],
    "career": [
        "rabotat_spb", "moskva1717", "m_worker_open", "rabota_msk_chat",
        "Sochi_Jobs", "rabotakrasnodar11", "rabotaspb63", "rabota_msk_tg",
        "raznorabochieye_moskva", "krasnodarkubann", "rabotaekb2021",
        "ekb_rabota_podrabotka", "rabota_ekb_chat", "stroyka_msk_oblast",
        "rabota_v_moskva", "stroitelstvo_msk", "freelance_birzha",
        "frlnc_ru", "freelance_chatik0", "birzha_vakansiy",
        "pomogator_freelancer", "jobosphere", "moskva_vakhta",
        "vakansii_infobiznes", "tezwork_moskva", "freeassistant",
    ],
    "sport": [
        "zhfut", "GOAL24_Chat", "newsbarcachat", "chat_proefootball",
        "GOAL24FAM", "Banda_Lamparda8", "pfk_cska_chat",
        "fckrasnodarChat", "champchat2", "jivichatt", "okkosportchat",
        "rgchatik", "figurnoe_katanie", "match_biathlon", "rep_chess_msk",
    ],
}


def build_sources():
    sources = []
    for group, channels in CHANNELS.items():
        for name in channels:
            sources.append({
                "name": name,
                "type": "telegram",
                "enabled": True,
                "group": group,
            })
    data = {
        "version": 3,
        "updated_at": "2026-09-19",
        "total": len(sources),
        "sources": sources,
    }
    with open(SOURCES, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ sources.json обновлён: {len(sources)} каналов")
    for group, channels in CHANNELS.items():
        print(f"   {group}: {len(channels)}")


def update_config():
    config = {
        "max_words": 200,
        "min_authors": 2,
        "target_authors": 1,
        "min_message_words": 5,
        "skip_bots": True,
        "output_dir": "data/raw_threads",
        "database_path": "data/dialogues.db",
        "fetch": {
            "days_back": 30,
            "limit_per_channel": 5000,
            "batch_size": 50,
        },
    }
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"✅ mining_config.json обновлён")


if __name__ == "__main__":
    build_sources()
    update_config()
    print()
    print("Готово. Проверь:")
    print(f"  cat sources.json | head -20")
    print(f"  cat configs/mining_config.json")