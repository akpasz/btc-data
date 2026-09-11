import io, json
p = 'src/treasury/registry.json'
reg = json.load(io.open(p, encoding='utf-8'))
entry = {
  "instrument": "Convertible long-term notes (arrived with the 8 December 2025 business combination)",
  "attribution": "ambiguous",
  "seniority": "",
  "from": "2026-03-31",
  "until": None,
  "schedule": [
    {"from": "2026-03-31", "amount_usd": 484326591.0, "period": "2025-12-31"},
    {"from": "2026-05-13", "amount_usd": 484434554.0, "period": "2026-03-31"},
    {"from": "2026-08-11", "amount_usd": 484543716.0, "period": "2026-06-30"}
  ],
  "source": "0001213900-26-037460 10-K for 2025-12-31 and 2 later 10-Qs (carrying amounts). Attribution evidence: 0001213900-25-120736, 8-K filed 11 December 2025, recording that the business combination with Cantor Equity Partners, Tether Investments and iFinex closed on 8 December 2025.",
  "note": "AMBIGUOUS ON THE EVIDENCE, NOT AS A FALLBACK. The notes first appear on the 2025-12-31 balance sheet, weeks after the combination closed on 8 December 2025, so they arrived through the transaction structure rather than a standalone offering. There is no use-of-proceeds sentence of the usual kind to read: the question is what the notes funded within the combination, described in the S-4 filed 17 October 2025, which has not been read. TO NARROW IT: if the notes financed the bitcoin contributed at closing this becomes treasury; if they financed other consideration, operating.",
  "carrying_amount_not_face": True
}
for c in reg['companies']:
    if c['ticker'] == 'XXI':
        c['claims'] = [entry]
        c['confidence'] = 'medium'
io.open(p, 'w', encoding='utf-8').write(json.dumps(reg, indent=2, ensure_ascii=False))
print('XXI claim written')
