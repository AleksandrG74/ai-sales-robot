import json
from pathlib import Path

SOURCES = Path("/app/sources.json")

REMOVE = {
    "appetit_perm", "barista_yuga", "che_pochem_krasnoyarsk",
    "clean_cognitions", "forum_myasnika", "frlnc_ru", "grandshef_chat",
    "konditery_pekari", "match_biathlon", "mentally_pie",
    "obshepit_yaroslavl", "pfk_cska_chat", "pomogator_freelancer",
    "psiologwehelp", "rabota_ekb_chat", "raznorabochieye_moskva",
    "rep_chess_msk", "ryba_na_dom_krym", "shef_craba_nizhniy_novgorod",
    "stroyka_msk_oblast", "vvkusiteli", "zapret_z4r",
    "dizainerv_wb_ozon", "dizainerv_wbozon", "marketplaceesschat",
    "shef_kraba_izhevsk", "images_for_konditery",
    "wildbernes_marketplace_ozonchat",
}

ADD = [
    ("@rztkd_chat", "tech"), ("@radio_t_chat", "tech"),
    ("@chat_xtb", "tech"), ("@aezachat_ru", "tech"),
    ("@AntichatOfficial", "tech"), ("@byebyedpi_group", "tech"),
    ("@mtchatik", "tech"), ("@hostvds_cloud", "tech"),
    ("@devops_ru", "tech"), ("@kubernetes_ru", "tech"),
    ("@sqlcom", "tech"), ("@androidinsider_oficial", "tech"),
    ("@rudart", "tech"),
    ("@vkusvill_comm", "food"), ("@lovely_recepty", "food"),
    ("@myasoforum", "food"), ("@fishlook_chat", "food"),
    ("@recepty_tam", "food"),
    ("@freelance_chat_ru", "career"),
    ("@rpl_chat", "sport"), ("@zenit_chat_fc", "sport"),
    ("@psih_chat", "psychology"), ("@PsiologWeHelp", "psychology"),
    ("@wbnahodkychat", "marketplace"),
    ("@repetitor_repetitori", "education"),
    ("@studentsfortutors", "education"),
    ("@lessondeliverychat", "education"),
]

data = json.loads(SOURCES.read_text(encoding="utf-8"))
new_sources = []
removed = 0
for s in data["sources"]:
    if s["name"] in REMOVE:
        removed += 1
        continue
    new_sources.append(s)

added = 0
for name, group in ADD:
    new_sources.append({
        "name": name, "type": "telegram",
        "enabled": True, "group": group,
    })
    added += 1

data["sources"] = new_sources
data["total"] = len(new_sources)
OUTPUT = Path("/app/data/sources.json")
OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f": {removed}")
print(f": {added}")
print(f": {len(new_sources)}")
