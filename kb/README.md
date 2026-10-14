# Knowledge base

Fictional content for the Hvylia Mobile demo operator. Every document is in Ukrainian: it is
what the bot retrieves (SDD "База знань і RAG") and the ground truth for `evals/`. Brands,
tariffs and prices of real operators must never appear here.

This README is for developers and is not ingested.

## Layout

```
tariffs/<tariff_id>.md     one tariff per file
services/<service_id>.md   one activatable service per file
roaming.md                 roaming zones and out-of-package prices
faq.md                     balance, top-up, tariff change, porting, eSIM, support
```

The ingester splits documents by headings into 300–800 token chunks, so every `##` section
must stand on its own: repeat the tariff or service name in the heading and in the text, and
do not refer to "above" or "below".

Each tariff and service file states its id on a line `Код тарифу: \`<id>\`` or
`Код послуги: \`<id>\``. The LLM reads that id from retrieved chunks and passes it to
`activate_service(service_id)`.

## Catalog

The mock subscriber actions (ROZ-24) use exactly these ids, names and prices.
`packages/engine/tests/test_kb_content.py` checks that the documents match this table.

| Kind | Id | Name | Price |
|------|----|------|-------|
| tariff | `start` | Старт | 145 грн/міс |
| tariff | `optimum` | Оптимум | 235 грн/міс |
| tariff | `bezlim` | Безлім | 395 грн/міс |
| tariff | `simeinyi` | Сімейний | 640 грн/міс |
| tariff | `dytiachyi` | Дитячий | 115 грн/міс |
| service | `roaming_eu` | Роумінг Європа | 99 грн/міс |
| service | `roaming_world` | Роумінг Світ | 249 грн/7 днів |
| service | `extra_internet_10gb` | Додаткові 10 ГБ | 69 грн |
| service | `antispam` | Антиспам | 19 грн/міс |
| service | `call_forwarding` | Переадресація | 0 грн |
| service | `who_called` | Хто дзвонив | 9 грн/міс |
| service | `music` | Хвиля Музика | 49 грн/міс |
| service | `voicemail` | Голосова пошта | 15 грн/міс |

The tariff «Космос» deliberately does not exist: it is the "not found" case of scenario S2.
