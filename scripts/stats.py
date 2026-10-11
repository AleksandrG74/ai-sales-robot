import sqlite3, json
c = sqlite3.connect('/app/data/robot.db')
rows = c.execute('SELECT result_json FROM processed_dialogues').fetchall()
scores = []
cosines = []
roles = {}
aggr = {}
for (rj,) in rows:
    if not rj:
        continue
    d = json.loads(rj)
    s = d.get('score')
    co = d.get('cosine')
    if s is not None:
        scores.append(float(s))
    if co is not None:
        cosines.append(float(co))
    r = d.get('role') or 'unknown'
    a = d.get('aggression') or 'unknown'
    roles[r] = roles.get(r, 0) + 1
    aggr[a] = aggr.get(a, 0) + 1
print('processed:', len(rows))
if scores:
    print('avg score:', round(sum(scores)/len(scores), 4))
    print('min/max score:', min(scores), max(scores))
if cosines:
    print('avg cosine:', round(sum(cosines)/len(cosines), 4))
    print('min/max cosine:', min(cosines), max(cosines))
print('roles:', roles)
print('aggression:', aggr)
